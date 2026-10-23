"""Shared types used by every component of the suite."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    NEEDS_REVIEW = "needs_review"


class DecidedBy(StrEnum):
    RULE = "rule"
    HUMAN = "human"
    NONE = "none"


ErrorKind = Literal[
    "http_status",
    "timeout",
    "transport",
    "empty_reply",
    "unreadable_reply",
    "budget_exhausted",
    "aborted",
    "check_error",
    "not_run",
]


class ConfigError(Exception):
    """A user-facing problem with an input file or run folder.

    The CLI prints its message as-is.
    """


class SendError(Exception):
    """A failed attempt to get a reply from the target."""

    def __init__(
        self,
        kind: ErrorKind,
        detail: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class Attempt:
    """One send of one prompt. `attempt_id` is `f"{prompt_id}#{repeat_index}"`.

    `repeat_index` starts at 1.
    """

    attempt_id: str
    prompt_id: str
    repeat_index: int
    text: str


@dataclass(frozen=True)
class SendOutcome:
    """The result of sending one attempt. `started_at` is an ISO-8601 UTC string."""

    attempt: Attempt
    reply: str | None
    error: SendError | None
    latency_ms: int
    started_at: str


@dataclass(frozen=True)
class Judgement:
    """A verdict with its reasons. `hint` is "refusal", "no_refusal" or ""."""

    verdict: Verdict
    decided_by: DecidedBy
    reasons: tuple[str, ...]
    hint: str


SEVERITY: dict[Verdict, int] = {
    Verdict.FAIL: 3,
    Verdict.INCONCLUSIVE: 2,
    Verdict.NEEDS_REVIEW: 1,
    Verdict.PASS: 0,
}


def worst(verdicts: Iterable[Verdict]) -> Verdict:
    """Return the most severe verdict. An empty input is INCONCLUSIVE, never PASS."""
    return max(verdicts, key=SEVERITY.__getitem__, default=Verdict.INCONCLUSIVE)
