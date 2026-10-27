"""Compare two runs: what a change fixed, broke or left alone.

The usual use is one bot before and after a change, e.g. the lab bot with its guardrails off
(the baseline) and on. Each prompt present in both runs gets one :class:`Change`:

* ``fixed``: Fail in the baseline, Pass now; ``regressed``: Pass in the baseline, Fail now;
* ``unchanged``: the same verdict on both sides;
* ``unclear``: any other difference, e.g. Fail to Inconclusive. Inconclusive is never a Pass, so
  moving into or out of it is neither a fix nor a regression;
* ``new`` / ``removed``: the prompt is only in the newer run / only in the baseline.

Both runs are read as *final* reports (:func:`suite.report.build_report_data` with
``final=True``), so an attempt still waiting for review counts as Inconclusive on either side.
:func:`write_compare` writes ``compare.md`` into the newer run's folder and nothing else.
"""

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from suite.evidence import read_run_json
from suite.models import ConfigError, Verdict
from suite.report import build_report_data, md_cell, verdict_label

COMPARE_MD = "compare.md"

_ABSENT = "—"


class Change(StrEnum):
    FIXED = "fixed"
    REGRESSED = "regressed"
    UNCHANGED = "unchanged"
    UNCLEAR = "unclear"
    NEW = "new"
    REMOVED = "removed"


@dataclass(frozen=True)
class CompareRow:
    """One prompt in the comparison. A verdict is ``None`` on the side that lacks the prompt."""

    prompt_id: str
    category: str
    baseline: Verdict | None
    current: Verdict | None
    change: Change


def compare_runs(baseline_dir: Path, run_dir: Path) -> list[CompareRow]:
    """Compare the run in ``run_dir`` against the one in ``baseline_dir``, prompt by prompt.

    Rows follow the newer run's prompt order, then the removed prompts in the baseline's order.
    A prompt's verdict is the worst over its attempts. Raises ConfigError if both are the same
    folder, or if either is not a run folder (no ``run.json`` or ``evidence.db``).
    """
    if baseline_dir.resolve() == run_dir.resolve():
        raise ConfigError("baseline and run are the same folder")
    baseline = build_report_data(baseline_dir, final=True).prompts
    current = build_report_data(run_dir, final=True).prompts

    before = {prompt.prompt_id: prompt for prompt in baseline}
    rows: list[CompareRow] = []
    for prompt in current:
        old = before.get(prompt.prompt_id)
        change = Change.NEW if old is None else _classify(old.verdict, prompt.verdict)
        rows.append(
            CompareRow(
                prompt.prompt_id,
                prompt.category,
                None if old is None else old.verdict,
                prompt.verdict,
                change,
            )
        )
    now = {prompt.prompt_id for prompt in current}
    rows.extend(
        CompareRow(prompt.prompt_id, prompt.category, prompt.verdict, None, Change.REMOVED)
        for prompt in baseline
        if prompt.prompt_id not in now
    )
    return rows


def endpoint_name(run: dict[str, Any]) -> str | None:
    """The endpoint's name from a ``run.json`` dict, or None if it has none."""
    endpoint = run.get("endpoint")
    name = endpoint.get("name") if isinstance(endpoint, dict) else None
    return name if isinstance(name, str) and name else None


def write_compare(
    run_dir: Path,
    rows: list[CompareRow],
    *,
    baseline_id: str,
    baseline_endpoint: str | None = None,
) -> Path:
    """Write ``compare.md`` into ``run_dir`` (the newer run) and return its path.

    The file has a heading naming both run ids, a counts line, a note if the baseline's endpoint
    (``baseline_endpoint``, see :func:`endpoint_name`) differs from this run's, and a table of the
    rows that are not unchanged. Every value that came from a run goes through :func:`md_cell`.
    Raises ConfigError if ``run_dir`` has no readable ``run.json``.
    """
    run = read_run_json(run_dir)
    run_id = str(run.get("run_id") or run_dir.name)
    counts = Counter(row.change for row in rows)

    lines = [
        f"# Compare: {md_cell(baseline_id)} → {md_cell(run_id)}",
        "",
        " · ".join(f"{change.value} {counts[change]}" for change in Change),
        "",
    ]
    current_endpoint = endpoint_name(run)
    if baseline_endpoint and current_endpoint and baseline_endpoint != current_endpoint:
        lines += [
            "Note: these runs used different endpoints "
            f"({md_cell(baseline_endpoint)} vs {md_cell(current_endpoint)}).",
            "",
        ]
    changed = [row for row in rows if row.change is not Change.UNCHANGED]
    if changed:
        lines += [
            "| Prompt | Category | Baseline | This run | Change |",
            "| --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| {md_cell(row.prompt_id)} | {md_cell(row.category)} | {_word(row.baseline)} "
            f"| {_word(row.current)} | {row.change.value} |"
            for row in changed
        ]
    else:
        lines.append("No prompt changed.")

    path = run_dir / COMPARE_MD
    # newline="": no CRLF translation on Windows, so the file is the same everywhere.
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
    return path


def _classify(before: Verdict, after: Verdict) -> Change:
    if before is after:
        return Change.UNCHANGED
    if (before, after) == (Verdict.FAIL, Verdict.PASS):
        return Change.FIXED
    if (before, after) == (Verdict.PASS, Verdict.FAIL):
        return Change.REGRESSED
    return Change.UNCLEAR


def _word(verdict: Verdict | None) -> str:
    return _ABSENT if verdict is None else verdict_label(verdict)
