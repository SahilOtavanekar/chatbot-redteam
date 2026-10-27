"""Run discovery for the web UI: list the run folders and resolve one safely.

This is the web layer's door to the engine. It reads run folders through ``suite.evidence`` and
hands back plain data, so the views never touch SQLite or ``run.json`` directly. A run id comes
from a URL, so it is untrusted: ``resolve_run_dir`` is the only way to turn one into a path.
"""

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from suite.evidence import read_run_json
from suite.models import ConfigError
from suite.prompts import OWASP_LLM_2025
from suite.report import VERDICT_LEGEND, ReportData, build_report_data, run_facts
from suite.web.settings import WebSettings

UNREADABLE = "unreadable"


@dataclass(frozen=True)
class RunSummary:
    """One row of the run list.

    ``totals`` is the as-run tally from ``run.json`` (before any human review), and
    ``finalized`` is true once a review has been folded back into the run. A folder whose
    ``run.json`` is missing or broken is still listed, with ``readable=False``, rather than
    hidden or raised.
    """

    run_id: str
    endpoint_name: str | None
    status: str
    started_at: str | None
    finalized: bool
    totals: dict[str, int]
    readable: bool


@dataclass(frozen=True)
class RunDetail:
    """Everything the run page shows: the report data, the header facts and the verdict legend."""

    run_id: str
    report: ReportData
    facts: list[tuple[str, str]]
    legend: str


@dataclass(frozen=True)
class CoverageRow:
    """One OWASP LLM Top 10 category: whether the run has any prompts for it, and how many."""

    code: str
    name: str
    present: bool
    prompt_count: int


def resolve_run_dir(settings: WebSettings, run_id: str) -> Path | None:
    """Return the run folder for ``run_id``, or ``None`` if it is not a safe, existing run folder.

    A run id must name a direct child directory of ``results_dir``. An empty id, one with a path
    separator, drive or ``..``, and one that resolves outside ``results_dir`` (a symlink, say) all
    return ``None``, and so does a folder that does not exist.
    """
    if not run_id or run_id in {".", ".."} or Path(run_id).name != run_id:
        return None
    try:
        root = settings.results_dir.resolve()
        candidate = (root / run_id).resolve()
        if candidate.parent != root or not candidate.is_dir():
            return None
    except (OSError, ValueError):  # e.g. an id with a NUL byte, or an unreadable path
        return None
    return settings.results_dir / run_id


def list_runs(settings: WebSettings) -> list[RunSummary]:
    """Summarise every run folder under ``results_dir``, newest ``started_at`` first.

    Ties (and runs with no start time, which sort last) fall back to the folder name. A missing
    ``results_dir`` is an empty list. Only folders ``resolve_run_dir`` accepts are listed, so
    every listed id can be opened.
    """
    try:
        names = [entry.name for entry in settings.results_dir.iterdir()]
    except OSError:
        return []
    runs = [
        _summarise(run_id, run_dir)
        for run_id in names
        if (run_dir := resolve_run_dir(settings, run_id)) is not None
    ]
    runs.sort(key=lambda run: run.run_id)
    runs.sort(key=lambda run: run.started_at or "", reverse=True)  # stable: name order on ties
    return runs


def _summarise(run_id: str, run_dir: Path) -> RunSummary:
    try:
        run = read_run_json(run_dir)
    except ConfigError:
        return RunSummary(run_id, None, UNREADABLE, None, False, {}, False)
    return RunSummary(
        run_id=run_id,
        endpoint_name=_endpoint_name(run),
        status=str(run.get("status", "unknown")),
        started_at=_text(run.get("started_at")),
        finalized=bool(run.get("finalized_at")),
        totals=_totals(run.get("summary")),
        readable=True,
    )


def _endpoint_name(run: dict[str, Any]) -> str | None:
    endpoint = run.get("endpoint")
    return _text(endpoint.get("name")) if isinstance(endpoint, dict) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _totals(summary: object) -> dict[str, int]:
    if not isinstance(summary, dict):
        return {}
    return {str(key): count for key, count in summary.items() if isinstance(count, int)}


def get_run(settings: WebSettings, run_id: str) -> RunDetail | None:
    """Return the detail for one run, or ``None`` if it is unknown or has no readable evidence.

    The report is built the way ``suite finalize`` would label it: final once the run has a
    ``finalized_at``, otherwise a draft whose queued attempts still count as awaiting review.
    """
    report = _load_report(settings, run_id)
    if report is None:
        return None
    return RunDetail(run_id, report, run_facts(report.run), VERDICT_LEGEND)


def coverage(settings: WebSettings, run_id: str) -> list[CoverageRow] | None:
    """Return one row per OWASP LLM Top 10 category, in order, or ``None`` for an unknown run.

    A category is ``present`` when at least one prompt in the run is tagged with it. Counts are
    prompts, not attempts. A category with no prompts is a gap in coverage, not a pass.
    """
    report = _load_report(settings, run_id)
    if report is None:
        return None
    counts = Counter(prompt.category for prompt in report.prompts)
    return [
        CoverageRow(code, name, counts[code] > 0, counts[code])
        for code, name in OWASP_LLM_2025.items()
    ]


def _load_report(settings: WebSettings, run_id: str) -> ReportData | None:
    run_dir = resolve_run_dir(settings, run_id)
    if run_dir is None:
        return None
    try:
        final = bool(read_run_json(run_dir).get("finalized_at"))
        return build_report_data(run_dir, final=final)
    except ConfigError:  # run.json or evidence.db missing or unreadable
        return None
