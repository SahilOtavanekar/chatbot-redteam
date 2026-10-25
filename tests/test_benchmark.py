"""Benchmark guarding spec N1: 100 prompts at concurrency 50 against a 1 s bot finish in < 6 s.

Executed: real engine + real PyRIT HTTP sender + the local mock bot. The mock bot delays every
reply by one second, so a perfectly parallel run takes about two seconds (two waves of 50) and
a run that serialises anywhere takes far longer than the limit.
"""

import time

from suite.adapters import build_sender
from suite.engine import RateLimiter, run_attempts
from suite.models import SendOutcome
from suite.planner import build_attempts
from tests.factories import make_pack, make_prompt, make_spec


async def test_100_prompts_at_50_concurrency_under_6s(mockbot, pyrit_ready):
    url, state = mockbot
    state.delay_s = 1.0
    spec = make_spec(url=f"{url}/chat", limits=dict(concurrency=50, requests_per_minute=100_000))
    attempts = build_attempts(
        make_pack(make_prompt("B", repeat=20), *[make_prompt(f"P{i}", repeat=20) for i in range(4)])
    )
    outcomes: list[SendOutcome] = []

    async def collect(outcome: SendOutcome) -> None:
        outcomes.append(outcome)

    sender = build_sender(spec)
    try:
        t0 = time.perf_counter()
        stats = await run_attempts(
            attempts, sender, spec.limits, on_outcome=collect, limiter=RateLimiter(100_000)
        )
        elapsed = time.perf_counter() - t0
    finally:
        await sender.aclose()
    print(f"N1 benchmark: {elapsed:.2f} s, peak in-flight {state.peak_in_flight}")
    assert stats.sent_requests == 100
    assert len(outcomes) == 100
    assert all(o.error is None for o in outcomes)
    assert state.peak_in_flight == 50
    assert elapsed < 6.0
