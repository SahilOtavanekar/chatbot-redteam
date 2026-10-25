"""The per-run evidence store (``evidence.db``) and the ``run.json`` helpers.

The store is our own SQLite file, written with the standard library, so reports, review and
compare never depend on PyRIT's schema. It keeps everything exactly as it happened: prompt text
verbatim and raw, unredacted replies. Redaction happens only when a report is rendered.

``add_attempt`` commits on every call, so evidence survives a crash or Ctrl+C part-way through a
run. The store holds one connection and is meant for one thread (the engine's event loop).
"""

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from suite.models import ConfigError, DecidedBy, Judgement, SendOutcome, Verdict
from suite.prompts import Pack

RUN_JSON = "run.json"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prompts(prompt_id TEXT PRIMARY KEY, category TEXT NOT NULL,
  prompt_text TEXT NOT NULL, pass_if TEXT NOT NULL, checks_json TEXT NOT NULL,
  repeat INTEGER NOT NULL, position INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS attempts(attempt_id TEXT PRIMARY KEY,
  prompt_id TEXT NOT NULL REFERENCES prompts(prompt_id), repeat_index INTEGER NOT NULL,
  reply_text TEXT, verdict TEXT NOT NULL, decided_by TEXT NOT NULL, reasons_json TEXT NOT NULL,
  hint TEXT NOT NULL, error_kind TEXT, http_status INTEGER, latency_ms INTEGER NOT NULL,
  started_at TEXT NOT NULL, human_note TEXT NOT NULL DEFAULT '');
"""


@dataclass(frozen=True)
class PromptRow:
    """A prompt as stored. ``position`` is its 1-based place in the pack."""

    prompt_id: str
    category: str
    prompt_text: str
    pass_if: str
    checks: dict[str, Any]
    repeat: int
    position: int


@dataclass(frozen=True)
class AttemptRow:
    attempt_id: str
    prompt_id: str
    repeat_index: int
    reply_text: str | None
    verdict: Verdict
    decided_by: DecidedBy
    reasons: tuple[str, ...]
    hint: str
    error_kind: str | None
    http_status: int | None
    latency_ms: int
    started_at: str
    human_note: str


class EvidenceStore:
    """One run's prompts and attempts, in the SQLite file at ``path`` (created if new)."""

    def __init__(self, path: Path) -> None:
        try:
            self._conn = sqlite3.connect(path)
        except sqlite3.Error as exc:
            raise ConfigError(f"{path}: cannot open the evidence store ({exc})") from None
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            self._conn.close()
            raise ConfigError(f"{path}: cannot open the evidence store ({exc})") from None

    def add_prompts(self, pack: Pack) -> None:
        rows = [
            (
                prompt.id,
                prompt.category,
                prompt.prompt,
                prompt.pass_if,
                json.dumps(prompt.checks.model_dump(), ensure_ascii=False),
                prompt.repeat,
                position,
            )
            for position, prompt in enumerate(pack.prompts, start=1)
        ]
        with self._conn:
            self._conn.executemany("INSERT INTO prompts VALUES (?, ?, ?, ?, ?, ?, ?)", rows)

    def add_attempt(self, outcome: SendOutcome, judgement: Judgement) -> None:
        """Store one finished attempt and commit it. The reply is kept raw (unredacted)."""
        error = outcome.error
        reasons = [_storable(reason) for reason in judgement.reasons]
        row = (
            outcome.attempt.attempt_id,
            outcome.attempt.prompt_id,
            outcome.attempt.repeat_index,
            None if outcome.reply is None else _storable(outcome.reply),
            judgement.verdict.value,
            judgement.decided_by.value,
            json.dumps(reasons, ensure_ascii=False),
            judgement.hint,
            None if error is None else error.kind,
            None if error is None else error.status,
            outcome.latency_ms,
            outcome.started_at,
        )
        with self._conn:
            self._conn.execute(
                "INSERT INTO attempts(attempt_id, prompt_id, repeat_index, reply_text, verdict,"
                " decided_by, reasons_json, hint, error_kind, http_status, latency_ms, started_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )

    def set_human_verdict(self, attempt_id: str, verdict: Verdict, note: str) -> None:
        """Record a person's decision on a queued attempt (or change their own earlier one).

        Raises ConfigError for an unknown attempt, for an attempt a rule already decided, and
        for ``Verdict.NEEDS_REVIEW`` (that is the queue, not a decision).
        """
        if verdict is Verdict.NEEDS_REVIEW:
            raise ConfigError(
                f"attempt {attempt_id}: a review decision must be pass, fail or inconclusive, "
                "not needs_review"
            )
        with self._conn:
            # The rule is part of the UPDATE itself, so deciding and writing are one statement.
            updated = self._conn.execute(
                "UPDATE attempts SET verdict = ?, decided_by = ?, human_note = ?"
                " WHERE attempt_id = ? AND (verdict = ? OR decided_by = ?)",
                (
                    verdict.value,
                    DecidedBy.HUMAN.value,
                    note,
                    attempt_id,
                    Verdict.NEEDS_REVIEW.value,
                    DecidedBy.HUMAN.value,
                ),
            )
            if updated.rowcount == 0:
                known = self._conn.execute(
                    "SELECT 1 FROM attempts WHERE attempt_id = ?", (attempt_id,)
                ).fetchone()
                if known is None:
                    raise ConfigError(f"unknown attempt {attempt_id}")
                raise ConfigError(
                    f"attempt {attempt_id} was decided by rule; only queued items can be reviewed"
                )

    def prompts(self) -> list[PromptRow]:
        """Every prompt, in pack order."""
        rows = self._conn.execute(
            "SELECT prompt_id, category, prompt_text, pass_if, checks_json, repeat, position"
            " FROM prompts ORDER BY position"
        ).fetchall()
        return [PromptRow(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5], r[6]) for r in rows]

    def attempts(self) -> list[AttemptRow]:
        """Every attempt, ordered by its prompt's pack position, then repeat index."""
        rows = self._conn.execute(
            "SELECT a.attempt_id, a.prompt_id, a.repeat_index, a.reply_text, a.verdict,"
            " a.decided_by, a.reasons_json, a.hint, a.error_kind, a.http_status, a.latency_ms,"
            " a.started_at, a.human_note"
            " FROM attempts a JOIN prompts p ON p.prompt_id = a.prompt_id"
            " ORDER BY p.position, a.repeat_index"
        ).fetchall()
        return [
            AttemptRow(
                attempt_id=r[0],
                prompt_id=r[1],
                repeat_index=r[2],
                reply_text=r[3],
                verdict=Verdict(r[4]),
                decided_by=DecidedBy(r[5]),
                reasons=tuple(json.loads(r[6])),
                hint=r[7],
                error_kind=r[8],
                http_status=r[9],
                latency_ms=r[10],
                started_at=r[11],
                human_note=r[12],
            )
            for r in rows
        ]

    def close(self) -> None:
        """Close the file. Safe to call more than once."""
        self._conn.close()


def _storable(text: str) -> str:
    """Text that SQLite can hold.

    A lone surrogate (a bot's JSON reply can carry one, as the escape ``\\ud83d``) cannot be
    encoded as UTF-8 and would make the insert raise, losing the attempt. It is kept as visible
    ``\\ud83d`` text instead. Any other string is returned unchanged.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", errors="backslashreplace").decode("utf-8")
    return text


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_run_id(endpoint_name: str, now: datetime) -> str:
    return f"{now:%Y%m%d-%H%M%S}-{endpoint_name}"


def write_run_json(run_dir: Path, data: dict[str, Any]) -> None:
    """Write ``run.json`` atomically: a crash mid-write never leaves a truncated file."""
    # Data that is not JSON-serialisable fails here, before any file exists. A lone surrogate
    # passes (ensure_ascii=False) and fails later, at the write; the cleanup below covers that.
    text = json.dumps(data, indent=2, ensure_ascii=False)
    descriptor, temp_name = tempfile.mkstemp(dir=run_dir, prefix=f"{RUN_JSON}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, run_dir / RUN_JSON)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def read_run_json(run_dir: Path) -> dict[str, Any]:
    """Read ``run.json``. Raises ConfigError if it is missing, unreadable or not a JSON object."""
    path = run_dir / RUN_JSON
    if not path.is_file():
        raise ConfigError(f"{run_dir}: no {RUN_JSON} found; is this a run folder?")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: cannot read file ({exc.__class__.__name__})") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: not valid JSON ({exc.msg} at line {exc.lineno})") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a JSON object at the top level")
    return data
