"""The judge's ready-made detectors: PyRIT's regex scorers behind our ``Detector`` interface.

The four scorers are pure regex (no LLM, no network). Scoring still records each score in
PyRIT's central memory, so ``init_pyrit()`` must have run before a detector is called.
"""

from pyrit.score import (
    CredentialLeakScorer,
    MarkdownInjectionScorer,
    Scorer,
    ShellCommandOutputScorer,
    XSSOutputScorer,
)

from suite.judge import Detector


def _detector(name: str, scorer: Scorer) -> Detector:
    async def detect(text: str) -> str | None:
        scores = await scorer.score_text_async(text)
        if scores[0].get_value():
            # PyRIT's rationale names the patterns that matched ("Matched: JWT Token"); it never
            # quotes the reply, so a reason is safe to keep in reports.
            return f"{name}: {scores[0].score_rationale}"
        return None

    return detect


def pyrit_detectors() -> dict[str, Detector]:
    """The detectors a prompt's ``checks`` may name, keyed exactly by ``DETECTOR_NAMES``."""
    return {
        "credential_leak": _detector("credential_leak", CredentialLeakScorer()),
        "xss_output": _detector("xss_output", XSSOutputScorer()),
        "markdown_injection": _detector("markdown_injection", MarkdownInjectionScorer()),
        "shell_command_output": _detector("shell_command_output", ShellCommandOutputScorer()),
    }
