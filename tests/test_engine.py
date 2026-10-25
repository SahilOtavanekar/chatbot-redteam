"""Tests for the async runner: rate limiter, concurrency, retries, budget and abort.

Every test is Executed (it runs the real engine against a FakeSender). Time never really
passes except in short sender/sink sleeps; the limiter and the retry sleep are injected.
"""

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta

import pytest

from suite import engine
from suite.config import Limits
from suite.engine import RateLimiter, RunStats, run_attempts
from suite.models import Attempt, SendError, SendOutcome
from tests.fakes import FakeSender

# --- helpers ---------------------------------------------------------------------------------


def make_attempts(n: int) -> list[Attempt]:
    return [Attempt(f"P-{i}#1", f"P-{i}", 1, f"text {i}") for i in range(n)]


class Collector:
    """An ``on_outcome`` sink that keeps what it is given."""

    def __init__(self) -> None:
        self.outcomes: list[SendOutcome] = []

    async def __call__(self, outcome: SendOutcome) -> None:
        self.outcomes.append(outcome)


class SleepLog:
    """An injectable ``sleep`` that records the requested delays and returns at once."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def kinds(outcomes: Iterable[SendOutcome]) -> Counter[str]:
    return Counter(o.error.kind if o.error else "reply" for o in outcomes)


async def reply_ok(text: str, n: int) -> str:
    return "ok"


def http_error(
    status: int, retry_after: float | None = None
) -> Callable[[str, int], Awaitable[str]]:
    async def script(text: str, n: int) -> str:
        raise SendError("http_status", f"HTTP {status}", status=status, retry_after=retry_after)

    return script


async def run(
    attempts: list[Attempt],
    sender: FakeSender,
    limits: Limits | None = None,
    *,
    sleep: SleepLog | None = None,
    limiter: RateLimiter | None = None,
    hard_timeout_s: float | None = None,
) -> tuple[RunStats, list[SendOutcome]]:
    """Run with a fast limiter and a no-wait retry sleep; fail rather than hang."""
    sink = Collector()
    async with asyncio.timeout(10):
        stats = await run_attempts(
            attempts,
            sender,
            limits or Limits(),
            on_outcome=sink,
            limiter=limiter or RateLimiter(100_000),
            sleep=sleep or SleepLog(),
            hard_timeout_s=hard_timeout_s,
        )
    return stats, sink.outcomes


# --- RateLimiter -----------------------------------------------------------------------------


async def test_limiter_allows_rpm_then_waits() -> None:
    t, sleeps = [0.0], []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
        t[0] += s

    lim = RateLimiter(2, clock=lambda: t[0], sleep=fake_sleep)
    for _ in range(3):
        await lim.acquire()
    assert sleeps == [60.0]


async def test_limiter_window_slides() -> None:
    t, sleeps = [0.0], []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
        t[0] += s

    lim = RateLimiter(2, clock=lambda: t[0], sleep=fake_sleep)
    await lim.acquire()  # start at t=0
    t[0] = 10.0
    await lim.acquire()  # start at t=10
    t[0] = 60.0
    await lim.acquire()  # the t=0 start has left the window: no wait
    assert sleeps == []
    await lim.acquire()  # window now holds t=10 and t=60: wait until t=10 expires
    assert sleeps == [10.0]
    assert t[0] == 70.0


async def test_limiter_never_grants_more_than_rpm_per_window_to_concurrent_callers() -> None:
    t = [0.0]
    granted: list[float] = []

    async def fake_sleep(s: float) -> None:
        t[0] += s

    lim = RateLimiter(2, clock=lambda: t[0], sleep=fake_sleep)

    async def take() -> None:
        await lim.acquire()
        granted.append(t[0])

    await asyncio.gather(*(take() for _ in range(6)))
    assert granted == [0.0, 0.0, 60.0, 60.0, 120.0, 120.0]


# --- concurrency -----------------------------------------------------------------------------


async def test_runs_in_parallel_up_to_concurrency() -> None:
    async def slow(text: str, n: int) -> str:
        await asyncio.sleep(0.05)
        return "ok"

    sender = FakeSender(slow)
    attempts = make_attempts(20)
    stats, outcomes = await run(attempts, sender, Limits(concurrency=5))
    assert sender.peak == 5
    assert len(outcomes) == 20
    assert sorted(o.attempt.attempt_id for o in outcomes) == sorted(a.attempt_id for a in attempts)
    assert all(o.reply == "ok" and o.error is None for o in outcomes)
    assert stats == RunStats(sent_requests=20, aborted=False, elapsed_s=stats.elapsed_s)


async def test_on_outcome_runs_inside_the_concurrency_slot() -> None:
    busy = peak = 0
    outcomes: list[SendOutcome] = []

    async def script(text: str, n: int) -> str:
        nonlocal busy, peak
        busy += 1
        peak = max(peak, busy)
        return "ok"

    async def sink(outcome: SendOutcome) -> None:
        nonlocal busy
        await asyncio.sleep(0.005)
        busy -= 1  # the attempt is only finished once its outcome is stored
        outcomes.append(outcome)

    await run_attempts(
        make_attempts(6),
        FakeSender(script),
        Limits(concurrency=2),
        on_outcome=sink,
        limiter=RateLimiter(100_000),
        sleep=SleepLog(),
    )
    assert len(outcomes) == 6
    assert peak == 2


async def test_no_attempts_is_a_clean_empty_run() -> None:
    sender = FakeSender(reply_ok)
    stats, outcomes = await run([], sender)
    assert outcomes == []
    assert sender.calls == []
    assert stats.sent_requests == 0
    assert stats.aborted is False


# --- retries ---------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 503])
async def test_retries_throttling_status_then_succeeds(status: int) -> None:
    async def script(text: str, n: int) -> str:
        if n == 1:
            raise SendError("http_status", f"HTTP {status}", status=status, retry_after=0)
        return "ok"

    sleeps = SleepLog()
    stats, outcomes = await run(make_attempts(1), FakeSender(script), sleep=sleeps)
    assert outcomes[0].reply == "ok"
    assert outcomes[0].error is None
    assert stats.sent_requests == 2
    assert sleeps.delays == [0]


async def test_persistent_429_is_inconclusive() -> None:
    sleeps = SleepLog()
    stats, outcomes = await run(
        make_attempts(1),
        FakeSender(http_error(429, retry_after=0)),
        Limits(max_retries=2),
        sleep=sleeps,
    )
    error = outcomes[0].error
    assert error is not None
    assert error.kind == "http_status"
    assert error.status == 429
    assert outcomes[0].reply is None
    assert stats.sent_requests == 3
    assert sleeps.delays == [0, 0]


async def test_other_statuses_are_not_retried() -> None:
    sender = FakeSender(http_error(500))
    stats, outcomes = await run(make_attempts(1), sender)
    assert stats.sent_requests == 1
    assert len(sender.calls) == 1
    assert kinds(outcomes) == {"http_status": 1}


async def test_zero_max_retries_never_retries() -> None:
    sender = FakeSender(http_error(429, retry_after=0))
    stats, _ = await run(make_attempts(1), sender, Limits(max_retries=0))
    assert stats.sent_requests == 1


async def test_retry_backoff_is_exponential_without_retry_after() -> None:
    async def script(text: str, n: int) -> str:
        if n <= 2:
            raise SendError("http_status", "HTTP 429", status=429)
        return "ok"

    sleeps = SleepLog()
    _, outcomes = await run(
        make_attempts(1), FakeSender(script), Limits(max_retries=2), sleep=sleeps
    )
    assert sleeps.delays == [2, 4]
    assert outcomes[0].reply == "ok"


@pytest.mark.parametrize(("retry_after", "expected"), [(7, 7), (0, 0), (3600, 30)])
async def test_retry_after_is_honoured_and_capped_at_30_seconds(
    retry_after: float, expected: float
) -> None:
    sleeps = SleepLog()
    await run(
        make_attempts(1),
        FakeSender(http_error(429, retry_after=retry_after)),
        Limits(max_retries=1),
        sleep=sleeps,
    )
    assert sleeps.delays == [expected]


async def test_a_send_error_from_the_sender_is_kept_as_is() -> None:
    original = SendError("empty_reply", "the reply was empty")

    async def script(text: str, n: int) -> str:
        raise original

    sender = FakeSender(script)
    _, outcomes = await run(make_attempts(1), sender)
    assert outcomes[0].error is original
    assert len(sender.calls) == 1


# --- timeout and transport errors ------------------------------------------------------------


async def test_hard_timeout_maps_to_timeout() -> None:
    async def hang(text: str, n: int) -> str:
        await asyncio.Event().wait()
        return "never"

    sender = FakeSender(hang)
    _, outcomes = await run(make_attempts(1), sender, hard_timeout_s=0.05)
    error = outcomes[0].error
    assert error is not None
    assert error.kind == "timeout"
    assert "0.05" in error.detail
    assert outcomes[0].reply is None
    assert sender.in_flight == 0  # the abandoned send was cancelled


async def test_hard_timeout_defaults_to_timeout_s_plus_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine, "TIMEOUT_GRACE_S", 0.04)

    async def hang(text: str, n: int) -> str:
        await asyncio.Event().wait()
        return "never"

    _, outcomes = await run(make_attempts(1), FakeSender(hang), Limits(timeout_s=0.01))
    error = outcomes[0].error
    assert error is not None
    assert error.kind == "timeout"
    assert "0.05 s" in error.detail


async def test_unexpected_exception_is_transport() -> None:
    async def script(text: str, n: int) -> str:
        raise ValueError("x")

    _, outcomes = await run(make_attempts(1), FakeSender(script))
    error = outcomes[0].error
    assert error is not None
    assert error.kind == "transport"
    assert "ValueError" in error.detail


# --- abort after consecutive errors ----------------------------------------------------------


async def test_aborts_after_consecutive_errors() -> None:
    sender = FakeSender(http_error(500))
    stats, outcomes = await run(
        make_attempts(10), sender, Limits(concurrency=1, abort_after_errors=3)
    )
    assert len(sender.calls) == 3
    assert kinds(outcomes) == {"http_status": 3, "aborted": 7}
    assert stats.aborted is True
    assert stats.sent_requests == 3
    aborted = next(o.error for o in outcomes if o.error and o.error.kind == "aborted")
    assert aborted.detail == "run aborted after 3 consecutive errors"


@pytest.mark.parametrize("failure", ["http_500", "transport", "timeout"])
async def test_http_transport_and_timeout_errors_all_count_toward_abort(failure: str) -> None:
    async def script(text: str, n: int) -> str:
        if failure == "http_500":
            raise SendError("http_status", "HTTP 500", status=500)
        if failure == "transport":
            raise ConnectionResetError("reset")
        await asyncio.Event().wait()
        return "never"

    sender = FakeSender(script)
    stats, _ = await run(
        make_attempts(6),
        sender,
        Limits(concurrency=1, abort_after_errors=2),
        hard_timeout_s=0.01,
    )
    assert len(sender.calls) == 2
    assert stats.aborted is True


async def test_empty_reply_errors_do_not_count_toward_abort() -> None:
    async def script(text: str, n: int) -> str:
        raise SendError("empty_reply", "the reply was empty")

    sender = FakeSender(script)
    stats, _ = await run(make_attempts(5), sender, Limits(concurrency=1, abort_after_errors=2))
    assert len(sender.calls) == 5
    assert stats.aborted is False


async def test_reply_resets_error_streak() -> None:
    async def script(text: str, n: int) -> str:
        if text == "text 2":
            return "ok"
        raise SendError("http_status", "HTTP 500", status=500)

    sender = FakeSender(script)
    stats, outcomes = await run(
        make_attempts(5), sender, Limits(concurrency=1, abort_after_errors=3)
    )
    assert stats.aborted is False
    assert len(sender.calls) == 5
    assert kinds(outcomes) == {"http_status": 4, "reply": 1}


async def test_retried_attempt_counts_as_one_error_in_the_streak() -> None:
    sender = FakeSender(http_error(429, retry_after=0))
    stats, _ = await run(
        make_attempts(2),
        sender,
        Limits(concurrency=1, max_retries=2, abort_after_errors=3),
    )
    assert stats.sent_requests == 6  # 2 attempts x (1 send + 2 retries)
    assert stats.aborted is False


async def test_abort_while_waiting_for_the_limiter_gives_the_budget_back() -> None:
    t = [0.0]
    limiter_busy = asyncio.Event()  # set once attempt "text 1" is queued in the limiter
    release = asyncio.Event()  # lets the limiter wait end, after the abort has happened
    outcomes: list[SendOutcome] = []

    async def blocked_sleep(seconds: float) -> None:
        limiter_busy.set()
        await release.wait()
        t[0] += seconds

    async def script(text: str, n: int) -> str:
        await limiter_busy.wait()
        raise SendError("http_status", "HTTP 500", status=500)

    async def sink(outcome: SendOutcome) -> None:
        outcomes.append(outcome)
        release.set()

    sender = FakeSender(script)
    async with asyncio.timeout(10):
        stats = await run_attempts(
            make_attempts(2),
            sender,
            Limits(concurrency=2, abort_after_errors=1),
            on_outcome=sink,
            limiter=RateLimiter(1, clock=lambda: t[0], sleep=blocked_sleep),
            sleep=SleepLog(),
        )
    assert sender.calls == ["text 0"]
    assert kinds(outcomes) == {"http_status": 1, "aborted": 1}
    assert stats.aborted is True
    assert stats.sent_requests == 1  # the queued attempt's reservation was returned


# --- budget ----------------------------------------------------------------------------------


async def test_budget_exhausted() -> None:
    sender = FakeSender(reply_ok)
    stats, outcomes = await run(make_attempts(5), sender, Limits(concurrency=1, max_requests=3))
    assert len(sender.calls) == 3
    assert stats.sent_requests == 3
    assert kinds(outcomes) == {"reply": 3, "budget_exhausted": 2}
    exhausted = next(o.error for o in outcomes if o.error and o.error.kind == "budget_exhausted")
    assert exhausted.detail == "max_requests 3 reached"
    assert stats.aborted is False  # running out of budget is not an error streak


class YieldingLimiter(RateLimiter):
    """A limiter whose ``acquire`` really suspends, as a busy limiter does under contention."""

    async def acquire(self) -> None:
        await asyncio.sleep(0)
        await super().acquire()


async def test_budget_cap_holds_under_concurrency() -> None:
    # Budget must be reserved before awaiting the limiter; checking first and counting after
    # lets every concurrent attempt pass the check and overspend max_requests.
    async def slow(text: str, n: int) -> str:
        await asyncio.sleep(0.02)
        return "ok"

    sender = FakeSender(slow)
    stats, outcomes = await run(
        make_attempts(10),
        sender,
        Limits(concurrency=10, max_requests=3),
        limiter=YieldingLimiter(100_000),
    )
    assert len(sender.calls) == 3
    assert stats.sent_requests == 3
    assert kinds(outcomes) == {"reply": 3, "budget_exhausted": 7}


async def test_retries_spend_budget_too() -> None:
    sender = FakeSender(http_error(429, retry_after=0))
    stats, outcomes = await run(make_attempts(1), sender, Limits(max_retries=5, max_requests=2))
    assert len(sender.calls) == 2
    assert stats.sent_requests == 2
    assert kinds(outcomes) == {"budget_exhausted": 1}
    assert outcomes[0].latency_ms >= 0


# --- outcome details -------------------------------------------------------------------------


async def test_outcome_has_utc_start_time_and_latency() -> None:
    async def slow(text: str, n: int) -> str:
        await asyncio.sleep(0.05)
        return "ok"

    before = datetime.now(UTC)
    _, outcomes = await run(make_attempts(1), FakeSender(slow))
    after = datetime.now(UTC)
    started = datetime.fromisoformat(outcomes[0].started_at)
    assert started.utcoffset() == timedelta(0)
    assert before <= started <= after
    assert 10 <= outcomes[0].latency_ms < 5000  # a generous band; not a timing test


async def test_attempt_that_was_never_sent_has_zero_latency_and_a_start_time() -> None:
    _, outcomes = await run(
        make_attempts(3), FakeSender(reply_ok), Limits(concurrency=1, max_requests=1)
    )
    skipped = [o for o in outcomes if o.error and o.error.kind == "budget_exhausted"]
    assert len(skipped) == 2
    for outcome in skipped:
        assert outcome.latency_ms == 0
        assert outcome.reply is None
        assert datetime.fromisoformat(outcome.started_at).utcoffset() == timedelta(0)


# --- cancellation and sink failure -----------------------------------------------------------


async def test_cancellation_propagates_and_stops_in_flight_sends() -> None:
    gate = asyncio.Event()

    async def hang(text: str, n: int) -> str:
        await gate.wait()
        return "ok"

    sender = FakeSender(hang)
    sink = Collector()
    task = asyncio.create_task(
        run_attempts(
            make_attempts(4),
            sender,
            Limits(concurrency=2),
            on_outcome=sink,
            limiter=RateLimiter(100_000),
            sleep=SleepLog(),
        )
    )
    async with asyncio.timeout(5):
        while sender.in_flight < 2:
            await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sender.in_flight == 0
    assert len(sender.calls) == 2
    assert sink.outcomes == []


async def test_sink_failure_propagates_and_stops_the_run() -> None:
    async def slow(text: str, n: int) -> str:
        await asyncio.sleep(0.01)
        return "ok"

    async def bad_sink(outcome: SendOutcome) -> None:
        raise RuntimeError("disk full")

    sender = FakeSender(slow)
    with pytest.raises(RuntimeError, match="disk full"):
        async with asyncio.timeout(5):
            await run_attempts(
                make_attempts(20),
                sender,
                Limits(concurrency=2),
                on_outcome=bad_sink,
                limiter=RateLimiter(100_000),
                sleep=SleepLog(),
            )
    sent_so_far = len(sender.calls)
    await asyncio.sleep(0.05)  # an orphaned task would keep sending
    assert len(sender.calls) == sent_so_far
    assert sender.in_flight == 0
    assert sent_so_far < 20
