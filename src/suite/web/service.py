"""Run discovery for the web UI: list the run folders and resolve one safely.

This is the web layer's door to the engine. It reads run folders through ``suite.evidence`` and
hands back plain data, so the views never touch SQLite or ``run.json`` directly. A run id comes
from a URL, so it is untrusted: ``resolve_run_dir`` is the only way to turn one into a path.

It is also the only door that writes: a reviewer's verdict goes into the run's ``review.csv``
(``set_review_verdict``) and ``finalize_run`` folds the sheet back into the run.
"""

import csv
import os
import tempfile
import threading
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from suite.compare import Change, CompareRow, compare_runs
from suite.evidence import read_run_json
from suite.models import ConfigError, DecidedBy, Verdict
from suite.prompts import OWASP_LLM_2025
from suite.report import (
    REVIEW_COLUMNS,
    VERDICT_LEGEND,
    ReportData,
    build_report_data,
    csv_cell,
    run_facts,
    verdict_label,
)
from suite.review import REVIEW_CSV, finalize
from suite.web.settings import WebSettings

UNREADABLE = "unreadable"

# What the views print about verdicts, re-exported here so a template never reaches into the engine.
LEGEND = VERDICT_LEGEND
NO_VERDICT = "—"

# One lock for everything that touches ``review.csv`` in this process: a read, a verdict write
# (read, change one row, replace the file) and a finalize. Held together they cannot interleave, so
# two verdicts never overwrite each other and a read never meets a half-done replace (on Windows
# an open file cannot be replaced). It also guards the csv field limit: csv's default is 128 KB,
# a full reply can be longer, and the limit is process-wide, so one thread must not restore it
# under another. Re-entrant, because a verdict write reads the file while it holds the lock.
_CSV_LIMIT = 2**31 - 1
_review_csv_lock = threading.RLock()

_REVIEW_VERDICTS = frozenset({"", "pass", "fail"})


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


@dataclass(frozen=True)
class CompareView:
    """Two runs compared prompt by prompt: the rows, and how many prompts had each change.

    ``counts`` has one entry per ``Change`` value (``fixed``, ``regressed``, ``unchanged``,
    ``unclear``, ``new``, ``removed``), zero included, so a view can read any of them directly.
    """

    baseline_id: str
    run_id: str
    rows: list[CompareRow]
    counts: dict[str, int]


@dataclass(frozen=True)
class ReviewItem:
    """One attempt in a run's review queue, as a row of ``review.csv``.

    ``reply`` is the full redacted reply, not an excerpt. ``verdict`` and ``note`` are blank until
    a reviewer fills them in.
    """

    attempt_id: str
    prompt_id: str
    category: str
    hint: str
    pass_if: str
    prompt: str
    reply: str
    verdict: str
    note: str


@dataclass(frozen=True)
class VerdictWrite:
    """The outcome of saving one verdict. On success ``item`` is the updated row and ``error`` is
    ``None``. Otherwise ``ok`` is false, ``item`` is ``None`` and ``error`` says why; the file is
    left exactly as it was."""

    ok: bool
    item: ReviewItem | None
    error: str | None


@dataclass(frozen=True)
class FinalizeOutcome:
    """The outcome of finalizing a run. On success ``applied`` is how many attempts got a human
    verdict now and ``pending`` how many are still unreviewed. Otherwise ``ok`` is false, both are
    ``None`` and ``error`` carries the engine's message (it lists every bad row at once)."""

    ok: bool
    applied: int | None
    pending: int | None
    error: str | None


def verdict_text(verdict: Verdict | None, decided_by: Iterable[DecidedBy] = ()) -> str:
    """The word a report prints for ``verdict`` (``Pass (rule)``, ``Fail``, ...), or a dash for
    ``None`` (a prompt that one side of a comparison lacks)."""
    return NO_VERDICT if verdict is None else verdict_label(verdict, decided_by)


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


def compare(settings: WebSettings, baseline_id: str, run_id: str) -> CompareView | None:
    """Compare ``run_id`` against ``baseline_id``, or return ``None`` if that cannot be done.

    Both ids go through ``resolve_run_dir``, so an unknown or unsafe id on either side is
    ``None``. So is a pair the engine refuses: the same run twice, or a folder without readable
    evidence. Both runs are read as final reports, as ``suite compare`` does.
    """
    baseline_dir = resolve_run_dir(settings, baseline_id)
    run_dir = resolve_run_dir(settings, run_id)
    if baseline_dir is None or run_dir is None:
        return None
    try:
        rows = compare_runs(baseline_dir, run_dir)
    except ConfigError:  # same folder, or run.json / evidence.db missing or unreadable
        return None
    tally = Counter(row.change.value for row in rows)
    counts = {change.value: tally[change.value] for change in Change}
    return CompareView(baseline_id, run_id, rows, counts)


def _load_report(settings: WebSettings, run_id: str) -> ReportData | None:
    run_dir = resolve_run_dir(settings, run_id)
    if run_dir is None:
        return None
    try:
        final = bool(read_run_json(run_dir).get("finalized_at"))
        return build_report_data(run_dir, final=final)
    except ConfigError:  # run.json or evidence.db missing or unreadable
        return None


def review_queue(settings: WebSettings, run_id: str) -> list[ReviewItem] | None:
    """Return the attempts a person must judge, from the run's ``review.csv``.

    The file is the draft queue ``suite run`` writes. A run with no ``review.csv`` has an empty
    queue (``[]``), and an unknown or unsafe ``run_id`` is ``None``. Rows come back in file order
    and a missing column reads as an empty string. Raises ConfigError if the file cannot be read
    (locked, or not valid UTF-8 / CSV).
    """
    run_dir = resolve_run_dir(settings, run_id)
    if run_dir is None:
        return None
    path = run_dir / REVIEW_CSV
    if not path.is_file():
        return []
    return [ReviewItem(**row) for row in _read_review_rows(path)]


def set_review_verdict(
    settings: WebSettings, run_id: str, attempt_id: str, verdict: str, note: str
) -> VerdictWrite:
    """Save a reviewer's ``verdict`` and ``note`` for one attempt in the run's ``review.csv``.

    ``verdict`` is ``pass``, ``fail`` or ``""`` (clear it), in any case; anything else is refused.
    The note goes through ``csv_cell``, so a note typed as ``=cmd()`` is stored as ``'=cmd()`` and
    never runs as a spreadsheet formula (``suite finalize`` strips that mark back off). Every
    other cell is written back exactly as read, under the header ``REVIEW_COLUMNS``. The new file
    replaces the old one atomically, so a failure leaves ``review.csv`` as it was.

    Not ``ok`` (and nothing written) for a bad verdict, an unknown or unsafe run, a run with no
    ``review.csv``, an ``attempt_id`` not in the file, or a file that cannot be read or replaced.
    An attempt listed on several rows has every one of them set, so the sheet stays consistent.
    """
    word = verdict.lower()
    if word not in _REVIEW_VERDICTS:
        return VerdictWrite(False, None, "verdict must be pass, fail or blank")
    # Trimmed on both sides, as ``suite finalize`` matches it: a padded cell the queue hands back
    # still matches, and a blank id never matches the blank rows a sheet can hold.
    attempt_id = attempt_id.strip()
    if not attempt_id:
        return VerdictWrite(False, None, f"attempt not found in {REVIEW_CSV}")
    run_dir = resolve_run_dir(settings, run_id)
    if run_dir is None:
        return VerdictWrite(False, None, "run not found")
    path = run_dir / REVIEW_CSV
    if not path.is_file():
        return VerdictWrite(False, None, f"{REVIEW_CSV} not found for this run")
    with _review_csv_lock:
        try:
            rows = _read_review_rows(path)
            matches = [row for row in rows if row["attempt_id"].strip() == attempt_id]
            if not matches:
                return VerdictWrite(False, None, f"attempt not found in {REVIEW_CSV}")
            for row in matches:
                row["verdict"] = word
                row["note"] = csv_cell(note)
            _write_review_rows(path, rows)
        except ConfigError as exc:
            return VerdictWrite(False, None, str(exc))
        except OSError as exc:  # on Windows, a file open in Excel cannot be replaced
            reason = exc.__class__.__name__
            return VerdictWrite(
                False, None, f"cannot write {REVIEW_CSV} ({reason}) — close it (e.g. in Excel)"
            )
    return VerdictWrite(True, ReviewItem(**matches[0]), None)


def finalize_run(settings: WebSettings, run_id: str) -> FinalizeOutcome:
    """Fold the run's ``review.csv`` into its evidence and write the final reports.

    This is ``suite finalize``. The engine applies all the verdicts or none, so a ConfigError
    (no ``review.csv``, a bad row, a report file locked in Excel) comes back as a not-``ok`` outcome
    carrying its message, never as an exception. An unknown or unsafe ``run_id`` is not ``ok`` too.
    """
    run_dir = resolve_run_dir(settings, run_id)
    if run_dir is None:
        return FinalizeOutcome(False, None, None, "run not found")
    # The engine reads review.csv itself and moves the process-wide csv limit without a lock, so
    # it runs inside ours: no queue read or verdict write can overlap it.
    with _review_csv_lock:
        try:
            result = finalize(run_dir)
        except ConfigError as exc:
            return FinalizeOutcome(False, None, None, str(exc))
    return FinalizeOutcome(True, result.applied, result.pending, None)


def _read_review_rows(path: Path) -> list[dict[str, str]]:
    """Every row of ``path`` as a dict keyed by ``REVIEW_COLUMNS``; a missing cell reads as ``""``.

    Header names are matched trimmed and lower-cased, as ``suite finalize`` matches them, so a
    sheet the engine accepts is read whole. Columns outside ``REVIEW_COLUMNS`` are ignored. The
    decode is strict: a replacement character must never be written back into the reviewer's file.
    Raises ConfigError if the file cannot be read or is not valid UTF-8 / CSV.
    """
    with _review_csv_lock:
        previous = csv.field_size_limit(_CSV_LIMIT)
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                reader.fieldnames = [name.strip().lower() for name in reader.fieldnames or []]
                return [
                    {column: row.get(column) or "" for column in REVIEW_COLUMNS} for row in reader
                ]
        except OSError as exc:
            raise ConfigError(
                f"{REVIEW_CSV}: cannot read file ({exc.__class__.__name__}); "
                "is it open in another program?"
            ) from None
        except UnicodeDecodeError:
            raise ConfigError(f"{REVIEW_CSV}: not valid UTF-8 text") from None
        except csv.Error as exc:
            raise ConfigError(f"{REVIEW_CSV}: not readable as CSV ({exc})") from None
        finally:
            csv.field_size_limit(previous)


def _write_review_rows(path: Path, rows: list[dict[str, str]]) -> None:
    """Replace ``path`` with ``rows`` under the ``REVIEW_COLUMNS`` header, atomically.

    The new file is written next to the old one and then renamed over it, so a crash or a locked
    target leaves the original intact and no temp file behind. ``utf-8-sig`` puts a BOM first,
    so Excel opens the file as UTF-8, as the engine's own writer does.
    """
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(REVIEW_COLUMNS)
            writer.writerows([row[column] for column in REVIEW_COLUMNS] for row in rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
