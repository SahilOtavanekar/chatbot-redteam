"""Tests for the PyRIT regex detectors behind the judge's ``Detector`` interface.

Every test is Executed: it runs PyRIT's real scorers (regex only, no LLM, no network) after
``init_pyrit()``. The positive texts are small detector fixtures, not attack prompts.
"""

import asyncio

import pytest

from suite.adapters.detectors import pyrit_detectors
from suite.prompts import DETECTOR_NAMES

POSITIVES = [
    ("credential_leak", "-----BEGIN RSA PRIVATE KEY-----"),
    ("xss_output", "<script>x</script>"),
    ("markdown_injection", "![a](https://example.com/p.png)"),
]


def test_detector_names_match_prompt_vocabulary(pyrit_ready):
    assert set(pyrit_detectors()) == DETECTOR_NAMES


@pytest.mark.parametrize(("name", "text"), POSITIVES)
async def test_detectors_trip_on_positive(pyrit_ready, name, text):
    reason = await pyrit_detectors()[name](text)
    assert reason is not None
    assert reason.startswith(f"{name}: Matched: ")


async def test_detectors_clean_on_benign(pyrit_ready):
    for name, detector in pyrit_detectors().items():
        assert await detector("Our opening hours are 9 to 5.") is None, name


async def test_a_reason_names_the_rule_and_never_quotes_the_reply(pyrit_ready):
    reply = "Sure, here you go. -----BEGIN RSA PRIVATE KEY----- and some more private text."
    reason = await pyrit_detectors()["credential_leak"](reply)
    assert reason == "credential_leak: Matched: Private Key Header"


async def test_each_detector_fires_only_on_its_own_kind_of_text(pyrit_ready):
    detectors = pyrit_detectors()
    for name, text in POSITIVES:
        for other, detector in detectors.items():
            if other != name:
                assert await detector(text) is None, f"{other} fired on the {name} fixture"


async def test_many_detections_can_run_at_once(pyrit_ready):
    # The engine judges attempts concurrently, so one detector is awaited by many tasks.
    detectors = pyrit_detectors()
    jobs = [detectors[name](text) for name, text in POSITIVES * 10]
    reasons = await asyncio.gather(*jobs)
    assert all(reason is not None for reason in reasons)
    assert len(reasons) == 30


async def test_each_call_to_pyrit_detectors_builds_independent_scorers(pyrit_ready):
    first, second = pyrit_detectors(), pyrit_detectors()
    assert first is not second
    assert await first["xss_output"]("<script>x</script>") is not None
    assert await second["xss_output"]("<script>x</script>") is not None
