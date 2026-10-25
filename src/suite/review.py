"""Human review: merge the verdicts a person typed into ``review.csv``, then write the final report.

``suite run`` leaves ``review.csv`` with one row per attempt that no automatic check could judge.
The reviewer reads each reply against its ``pass_if`` and types ``pass`` or ``fail`` in the
``verdict`` column (and, if they like, a ``note``). :func:`finalize` then:

1. reads the file and checks **every** row against the run's evidence, collecting all the
   problems it finds (an unknown attempt, a verdict that is neither ``pass`` nor ``fail``, an
   attempt a rule already decided);
2. only if there are none, records each verdict as a human decision. If there is any problem,
   nothing is applied and one :class:`ConfigError` lists them all, so the reviewer fixes the
   sheet once;
3. stamps ``finalized_at`` in ``run.json`` and writes the final reports.

A blank verdict leaves an attempt pending: the final report shows it as Inconclusive
(``not reviewed``), never as a pass. Finalizing again is safe: a person's earlier verdict can be
changed, and a blank cell keeps whatever was decided before. ``review.csv`` itself is only read.
"""

import csv
from dataclasses import dataclass
from pathlib import Path

from suite.evidence import AttemptRow, EvidenceStore, read_run_json, utc_now, write_run_json
from suite.models import ConfigError, DecidedBy, Verdict
from suite.report import EVIDENCE_DB, FORMULA_PREFIXES, write_reports

REVIEW_CSV = "review.csv"

_REQUIRED_COLUMNS = ("attempt_id", "verdict", "note")
_VERDICTS = {"pass": Verdict.PASS, "fail": Verdict.FAIL}
# csv refuses a cell over 128 KB by default; a long bot reply in review.csv would make the file
# we wrote unreadable. 2**31 - 1 is the most a C long (32-bit on Windows) can hold.
_CSV_FIELD_LIMIT = 2**31 - 1
_SHOWN_CHARS = 60


@dataclass(frozen=True)
class FinalizeResult:
    """``applied``: attempts given a human verdict now. ``pending``: attempts in the run that are
    still waiting for one (they appear as Inconclusive in the final report)."""

    applied: int
    pending: int


@dataclass(frozen=True)
class _Row:
    """A non-blank data row of review.csv. ``number`` is what a spreadsheet shows (header = 1)."""

    number: int
    attempt_id: str
    verdict: str
    note: str


@dataclass(frozen=True)
class _Decision:
    """One verdict to apply. ``rows`` are the spreadsheet rows it came from (more than one if the
    attempt was listed twice with the same verdict)."""

    rows: tuple[int, ...]
    attempt_id: str
    verdict: Verdict
    note: str


@dataclass(frozen=True)
class _Problem:
    row: int
    attempt_id: str
    text: str

    def __str__(self) -> str:
        return f"row {self.row} (attempt {_shown(self.attempt_id) or '(blank)'}): {self.text}"


def finalize(run_dir: Path) -> FinalizeResult:
    """Merge the verdicts in ``run_dir/review.csv`` and write the final reports.

    Raises ConfigError if ``review.csv``, ``run.json`` or ``evidence.db`` is missing or
    unreadable, if ``review.csv`` lacks the ``attempt_id``, ``verdict`` or ``note`` column, or if
    any row has a problem (nothing is applied then).
    """
    review_path = run_dir / REVIEW_CSV
    if not review_path.is_file():
        raise ConfigError(f"{REVIEW_CSV} not found in {run_dir}")
    run = read_run_json(run_dir)
    db_path = run_dir / EVIDENCE_DB
    # Checked first: EvidenceStore would create an empty database and finalize nothing.
    if not db_path.is_file():
        raise ConfigError(f"{run_dir}: no {EVIDENCE_DB} found; is this a run folder?")

    problems: list[_Problem] = []
    decisions = _decisions(_read_rows(review_path), problems)

    store = EvidenceStore(db_path)
    try:
        # Judged against the evidence before anything is written, so a bad row can never leave
        # the others half applied. The store re-checks each write as a backstop.
        known = {attempt.attempt_id: attempt for attempt in store.attempts()}
        for decision in decisions:
            reason = _not_reviewable(known.get(decision.attempt_id))
            if reason is not None:
                problems.extend(_Problem(row, decision.attempt_id, reason) for row in decision.rows)
        if problems:
            raise ConfigError(_describe(problems))
        for decision in decisions:
            store.set_human_verdict(decision.attempt_id, decision.verdict, decision.note)
        pending = sum(attempt.verdict is Verdict.NEEDS_REVIEW for attempt in store.attempts())
    finally:
        store.close()

    run["finalized_at"] = utc_now().isoformat()
    write_run_json(run_dir, run)
    write_reports(run_dir, final=True)
    return FinalizeResult(applied=len(decisions), pending=pending)


def _read_rows(path: Path) -> list[_Row]:
    """The non-blank data rows of review.csv.

    Excel-style files are fine: a BOM, CRLF line ends, short rows (missing cells read as blank)
    and trailing rows of empty cells (skipped).
    """
    previous = csv.field_size_limit(_CSV_FIELD_LIMIT)
    try:
        with path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
            records = list(csv.reader(handle))
    except OSError as exc:
        raise ConfigError(
            f"{path}: cannot read file ({exc.__class__.__name__}); is it open in another program?"
        ) from None
    except csv.Error as exc:
        raise ConfigError(f"{path}: not readable as CSV ({exc})") from None
    finally:
        csv.field_size_limit(previous)

    if not records:
        raise ConfigError(f"{REVIEW_CSV} is empty")
    header = [cell.strip().lower() for cell in records[0]]
    missing = [name for name in _REQUIRED_COLUMNS if name not in header]
    if missing:
        raise ConfigError(f"{REVIEW_CSV} is missing the column(s): {', '.join(missing)}")
    position = {name: header.index(name) for name in _REQUIRED_COLUMNS}

    rows: list[_Row] = []
    for number, record in enumerate(records[1:], start=2):
        if all(not cell.strip() for cell in record):
            continue
        rows.append(
            _Row(
                number,
                _cell(record, position["attempt_id"]).strip(),
                _cell(record, position["verdict"]),
                _cell(record, position["note"]),
            )
        )
    return rows


def _cell(record: list[str], index: int) -> str:
    """One cell of a CSV record; a short row reads as blank past its end."""
    return record[index] if index < len(record) else ""


def _decisions(rows: list[_Row], problems: list[_Problem]) -> list[_Decision]:
    """One decision per attempt that has a verdict, in first-seen order; problems are appended.

    A blank verdict is no decision. An attempt on several rows is fine if they agree (the last
    non-empty note wins) and a problem on each of those rows if they do not.
    """
    # attempt id -> (row, verdict, note) for each row that decides it
    by_attempt: dict[str, list[tuple[int, Verdict, str]]] = {}
    for row in rows:
        word = _verdict_word(row.verdict)
        if not word:
            continue
        if not row.attempt_id:
            problems.append(_Problem(row.number, "", "attempt_id is empty"))
            continue
        verdict = _VERDICTS.get(word)
        if verdict is None:
            problems.append(
                _Problem(
                    row.number,
                    row.attempt_id,
                    f"bad verdict '{_shown(row.verdict.strip())}'; use pass or fail, "
                    "or leave it blank",
                )
            )
            continue
        by_attempt.setdefault(row.attempt_id, []).append(
            (row.number, verdict, _clean_note(row.note))
        )

    decisions: list[_Decision] = []
    for attempt_id, entries in by_attempt.items():
        if len({verdict for _, verdict, _ in entries}) > 1:
            for number, verdict, _ in entries:
                others = ", ".join(str(n) for n, other, _ in entries if other is not verdict)
                problems.append(
                    _Problem(
                        number,
                        attempt_id,
                        f"gives a different verdict than row {others} for the same attempt",
                    )
                )
            continue
        note = next((note for _, _, note in reversed(entries) if note), "")
        decisions.append(
            _Decision(tuple(n for n, _, _ in entries), attempt_id, entries[0][1], note)
        )
    return decisions


def _verdict_word(cell: str) -> str:
    """The verdict as compared: trimmed, lower-cased, and without the ``'`` a spreadsheet may
    have kept in front (``'pass``)."""
    word = cell.strip()
    if word.startswith("'"):
        word = word[1:]
    return word.strip().lower()


def _clean_note(cell: str) -> str:
    """The note as typed, trimmed. One leading ``'`` goes only if the rest starts like a formula:
    that is the neutralising mark :func:`suite.report.csv_cell` adds, not the person's text."""
    note = cell.strip()
    if note.startswith("'") and note[1:].startswith(FORMULA_PREFIXES):
        note = note[1:]
    return note.strip()


def _not_reviewable(attempt: AttemptRow | None) -> str | None:
    """Why ``attempt`` cannot take a human verdict, or None. Mirrors the evidence store's rule:
    a queued attempt, or one a human already decided (re-finalize)."""
    if attempt is None:
        return "unknown attempt (it is not in this run)"
    if attempt.verdict is Verdict.NEEDS_REVIEW or attempt.decided_by is DecidedBy.HUMAN:
        return None
    if attempt.decided_by is DecidedBy.RULE:
        return "decided by rule; only queued items can be reviewed"
    return (
        f"not queued for review (it is {attempt.verdict.value}); only queued items can be reviewed"
    )


def _describe(problems: list[_Problem]) -> str:
    ordered = sorted(problems, key=lambda problem: problem.row)  # stable: ties keep their order
    count = len(ordered)
    head = (
        f"{REVIEW_CSV} has {count} {'problem' if count == 1 else 'problems'}; "
        "no verdicts were applied:"
    )
    return "\n".join([head, *map(str, ordered)])


def _shown(text: str) -> str:
    """Text from the file, made safe to print: control characters escaped, long text cut."""
    if not text.isprintable():
        text = text.encode("unicode_escape").decode("ascii")
    return text if len(text) <= _SHOWN_CHARS else text[:_SHOWN_CHARS] + "…"
