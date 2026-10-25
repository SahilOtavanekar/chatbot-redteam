"""Planning a run: expand prompts into attempts, then check the plan before anything is sent.

`preflight` is the gate both `suite validate` and `suite run` use. It refuses a plan whose
endpoint has no owner go-ahead reference, or that would send more requests than the endpoint's
`max_requests` allows. Nothing here sends a request.
"""

from dataclasses import dataclass

from suite.config import EndpointSpec, masked
from suite.models import Attempt
from suite.prompts import Pack


def build_attempts(pack: Pack) -> list[Attempt]:
    """One Attempt per prompt per repeat, in pack order; ``repeat_index`` runs 1..repeat."""
    return [
        Attempt(
            attempt_id=f"{prompt.id}#{repeat_index}",
            prompt_id=prompt.id,
            repeat_index=repeat_index,
            text=prompt.prompt,
        )
        for prompt in pack.prompts
        for repeat_index in range(1, prompt.repeat + 1)
    ]


@dataclass(frozen=True)
class PreflightReport:
    """What a run would do, and what stops it. ``authorized`` is None when it is absent or blank."""

    prompts: int
    attempts: int
    max_requests: int
    requests_per_minute: int
    concurrency: int
    authorized: str | None
    min_duration_s: float
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


def preflight(spec: EndpointSpec, pack: Pack) -> PreflightReport:
    """Check authorisation and request budget for running ``pack`` against ``spec``."""
    attempts = len(build_attempts(pack))
    limits = spec.limits
    authorized = spec.authorized if spec.authorized and spec.authorized.strip() else None

    problems: list[str] = []
    if authorized is None:
        problems.append(
            f"endpoint '{spec.name}' has no 'authorized' field "
            "— add the owner's go-ahead reference before running"
        )
    if attempts > limits.max_requests:
        problems.append(f"plan needs {attempts} requests but max_requests is {limits.max_requests}")

    # The rate limiter lets requests_per_minute start per minute, so the last attempt cannot
    # start before this long has passed. (max() keeps an empty plan at zero, not negative.)
    min_duration_s = 60.0 * (max(attempts - 1, 0) // limits.requests_per_minute)

    return PreflightReport(
        prompts=len(pack.prompts),
        attempts=attempts,
        max_requests=limits.max_requests,
        requests_per_minute=limits.requests_per_minute,
        concurrency=limits.concurrency,
        authorized=authorized,
        min_duration_s=min_duration_s,
        problems=tuple(problems),
    )


def format_preflight(report: PreflightReport, spec: EndpointSpec) -> str:
    """The report as one ``label: value`` line per fact, then one ``problem:`` line each.

    Never shows headers. The URL goes through ``masked`` so a secret substituted into it from
    the environment is shown as ``***``.
    """
    lines = [f"endpoint: {spec.name}", f"type: {spec.type}"]
    if spec.type == "http":
        lines.append(f"url: {masked(spec)['url']}")
    else:
        lines.append(f"level: {spec.level}")
    lines += [
        f"prompts: {report.prompts}",
        f"attempts: {report.attempts}",
        f"max_requests: {report.max_requests}",
        f"requests_per_minute: {report.requests_per_minute}",
        f"concurrency: {report.concurrency}",
        f"min_duration_s: {report.min_duration_s}",
        f"authorized: {report.authorized or 'MISSING'}",
    ]
    lines += [f"problem: {problem}" for problem in report.problems]
    return "\n".join(lines)
