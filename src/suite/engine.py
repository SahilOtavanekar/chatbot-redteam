"""The async runner: sends attempts in parallel under per-bot safety limits.

This is our own engine (not PyRIT's executor; see docs/pyrit-analysis.md). It enforces, per
run: a concurrency cap, a sliding-window requests-per-minute cap, a total request budget,
bounded retries of throttling responses, and an abort after repeated consecutive errors.

Every attempt ends in exactly one ``SendOutcome``, handed to ``on_outcome``. A failed or
unsent attempt carries a ``SendError`` and no reply; the judge later maps that to
Inconclusive, never Pass.
"""

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from suite.config import Limits
from suite.models import Attempt, SendError, SendOutcome

TIMEOUT_GRACE_S = 5.0  # added to Limits.timeout_s for the engine's own hard timeout
RETRY_STATUSES = frozenset({429, 503})  # throttling responses worth retrying
MAX_RETRY_DELAY_S = 30.0  # cap on one retry wait, also against a huge Retry-After
WINDOW_S = 60.0  # the rate limiter's sliding window
_COUNTED_KINDS = frozenset({"http_status", "timeout", "transport"})  # these feed the error streak


class Sender(Protocol):
    """Sends one prompt text to the target and returns the extracted reply."""

    async def send(self, text: str) -> str:
        """Return the reply text, or raise ``SendError``."""
        ...

    async def aclose(self) -> None: ...


OutcomeSink = Callable[[SendOutcome], Awaitable[None]]


class RateLimiter:
    """Sliding-window limiter: at most ``requests_per_minute`` starts in any 60 seconds."""

    def __init__(
        self,
        requests_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._rpm = requests_per_minute
        self._clock = clock
        self._sleep = sleep
        self._starts: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a request may start, then record the start."""
        # One waiter sleeps while holding the lock, so later callers queue behind it in order.
        async with self._lock:
            while True:
                now = self._clock()
                while self._starts and now - self._starts[0] >= WINDOW_S:
                    self._starts.popleft()
                if len(self._starts) < self._rpm:
                    self._starts.append(now)
                    return
                await self._sleep(WINDOW_S - (now - self._starts[0]))


@dataclass
class RunStats:
    sent_requests: int  # requests actually sent, retries included
    aborted: bool  # True if the run stopped early after repeated errors
    elapsed_s: float


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class _Run:
    """The state one ``run_attempts`` call shares between its concurrent attempts."""

    def __init__(
        self,
        sender: Sender,
        limits: Limits,
        limiter: RateLimiter,
        sleep: Callable[[float], Awaitable[None]],
        hard_timeout_s: float,
    ) -> None:
        self.sent = 0
        self.aborted = False
        self._streak = 0  # consecutive attempts that ended in a counted error
        self._sender = sender
        self._limits = limits
        self._limiter = limiter
        self._sleep = sleep
        self._hard_timeout_s = hard_timeout_s

    async def execute(self, attempt: Attempt) -> SendOutcome:
        limits = self._limits
        retries = 0
        started_at: str | None = None  # the first send; fallback is when the outcome is built
        latency_ms = 0  # the last send
        reply: str | None
        error: SendError | None
        while True:
            reply = None
            if self.aborted:
                error = self._abort_error()
                break
            if self.sent >= limits.max_requests:
                error = SendError("budget_exhausted", f"max_requests {limits.max_requests} reached")
                break
            # Reserve the budget before the first await. Attempts interleave at every await, so
            # counting after the limiter wait would let them all pass the check above together.
            self.sent += 1
            await self._limiter.acquire()
            if self.aborted:  # the run was aborted while this attempt waited for the limiter
                self.sent -= 1
                error = self._abort_error()
                break
            if started_at is None:
                started_at = _now_iso()
            began = time.perf_counter()
            reply, send_error = await self._send_once(attempt.text)
            latency_ms = round((time.perf_counter() - began) * 1000)
            if send_error is None:
                error = None
                break
            error = send_error
            if (
                error.kind == "http_status"
                and error.status in RETRY_STATUSES
                and retries < limits.max_retries
            ):
                retries += 1
                delay = error.retry_after if error.retry_after is not None else 2**retries
                await self._sleep(min(delay, MAX_RETRY_DELAY_S))
                continue
            break
        self._record(error)
        return SendOutcome(
            attempt=attempt,
            reply=reply,
            error=error,
            latency_ms=latency_ms,
            started_at=started_at or _now_iso(),
        )

    def _abort_error(self) -> SendError:
        return SendError(
            "aborted", f"run aborted after {self._limits.abort_after_errors} consecutive errors"
        )

    async def _send_once(self, text: str) -> tuple[str | None, SendError | None]:
        # CancelledError is a BaseException, so none of these handlers can swallow it.
        try:
            reply = await asyncio.wait_for(self._sender.send(text), self._hard_timeout_s)
        except SendError as exc:
            return None, exc
        except TimeoutError:
            return None, SendError("timeout", f"no reply within {self._hard_timeout_s:g} s")
        except Exception as exc:
            return None, SendError("transport", f"{type(exc).__name__}: {exc}")
        return reply, None

    def _record(self, error: SendError | None) -> None:
        """Update the consecutive-error streak from a finished attempt; abort at the limit."""
        if error is None:
            self._streak = 0
        elif error.kind in _COUNTED_KINDS:
            self._streak += 1
            if self._streak >= self._limits.abort_after_errors:
                self.aborted = True


async def run_attempts(
    attempts: Sequence[Attempt],
    sender: Sender,
    limits: Limits,
    *,
    on_outcome: OutcomeSink,
    limiter: RateLimiter,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    hard_timeout_s: float | None = None,
) -> RunStats:
    """Send every attempt, at most ``limits.concurrency`` at a time, and report each outcome.

    ``on_outcome`` is awaited once per attempt, inside that attempt's concurrency slot, so a
    slow sink slows the run instead of piling up work. Outcomes arrive in no fixed order.
    ``sleep`` is the wait between retries; ``hard_timeout_s`` defaults to
    ``limits.timeout_s + TIMEOUT_GRACE_S``. Cancellation and a failing sink propagate after every
    attempt still running has been cancelled; nothing is left sending in the background.
    """
    if hard_timeout_s is None:
        hard_timeout_s = limits.timeout_s + TIMEOUT_GRACE_S
    started = time.perf_counter()
    run = _Run(sender, limits, limiter, sleep, hard_timeout_s)
    slots = asyncio.Semaphore(limits.concurrency)

    async def one(attempt: Attempt) -> None:
        async with slots:
            await on_outcome(await run.execute(attempt))

    tasks = [asyncio.create_task(one(attempt)) for attempt in attempts]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return RunStats(
        sent_requests=run.sent, aborted=run.aborted, elapsed_s=time.perf_counter() - started
    )
