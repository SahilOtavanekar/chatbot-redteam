import csv
import os
import stat
from pathlib import Path

import pytest

from suite import review as review_module
from suite.evidence import EvidenceStore, read_run_json
from suite.models import ConfigError, DecidedBy, Verdict
from suite.report import REVIEW_COLUMNS, write_reports
from suite.review import FinalizeResult, finalize
from tests.factories import make_run_dir

CRLF = chr(13) + chr(10)

QUEUED = [
    ("P1#1", "LLM01", Verdict.NEEDS_REVIEW, "reply one"),
    ("P2#1", "LLM01", Verdict.NEEDS_REVIEW, "reply two"),
]


def queued_run(tmp_path: Path) -> Path:
    """A run folder with two queued attempts and the draft reports (so a review.csv exists)."""
    run_dir = make_run_dir(tmp_path, QUEUED)
    write_reports(run_dir, final=False)
    return run_dir


def review_path(run_dir: Path) -> Path:
    return run_dir / "review.csv"


def edit_review(
    run_dir: Path,
    edits: dict[str, tuple[str, str]],
    extra: list[dict[str, str]] | None = None,
) -> None:
    """Fill ``verdict`` and ``note`` for attempt ids (and append extra rows) in review.csv."""
    with review_path(run_dir).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        if row["attempt_id"] in edits:
            row["verdict"], row["note"] = edits[row["attempt_id"]]
    rows.extend(extra or [])
    with review_path(run_dir).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(REVIEW_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)


def extra_row(attempt_id: str, verdict: str, note: str = "") -> dict[str, str]:
    row = dict.fromkeys(REVIEW_COLUMNS, "")
    row.update(attempt_id=attempt_id, verdict=verdict, note=note)
    return row


def stored(run_dir: Path) -> dict[str, tuple[Verdict, DecidedBy, str]]:
    store = EvidenceStore(run_dir / "evidence.db")
    try:
        return {a.attempt_id: (a.verdict, a.decided_by, a.human_note) for a in store.attempts()}
    finally:
        store.close()


def problem_lines(error: pytest.ExceptionInfo[ConfigError]) -> list[str]:
    return [line for line in str(error.value).splitlines() if line.startswith("row ")]


# --- the happy path ----------------------------------------------------------------------------


def test_applies_verdicts_and_writes_final(tmp_path: Path) -> None:
    run_dir = queued_run(tmp_path)
    assert "finalized_at" not in read_run_json(run_dir)
    edit_review(run_dir, {"P1#1": ("pass", "looks safe"), "P2#1": ("fail", "leaked it")})

    result = finalize(run_dir)

    assert result == FinalizeResult(applied=2, pending=0)
    assert stored(run_dir) == {
        "P1#1": (Verdict.PASS, DecidedBy.HUMAN, "looks safe"),
        "P2#1": (Verdict.FAIL, DecidedBy.HUMAN, "leaked it"),
    }
    assert "FINAL" in (run_dir / "report.html").read_text(encoding="utf-8")
    assert "FINAL" in (run_dir / "report.md").read_text(encoding="utf-8")
    assert read_run_json(run_dir)["finalized_at"]
    with (run_dir / "matrix.csv").open(encoding="utf-8-sig", newline="") as handle:
        matrix = {row["attempt_id"]: row for row in csv.DictReader(handle)}
    assert (matrix["P1#1"]["verdict"], matrix["P1#1"]["decided_by"]) == ("pass", "human")
    assert (matrix["P2#1"]["verdict"], matrix["P2#1"]["decided_by"]) == ("fail", "human")


def test_excel_style_csv_accepted(tmp_path: Path) -> None:
    run_dir = queued_run(tmp_path)
    # What Excel writes back: BOM, CRLF line ends, odd case and spaces, and a trailing row of
    # empty cells (the sheet's used range).
    text = (
        ",".join(REVIEW_COLUMNS)
        + "\r\n"
        + "P1#1,P1,LLM01,,,prompt,reply one,PASS ,\r\n"
        + "P2#1,P2,LLM01,,,prompt,reply two, Fail,note\r\n"
        + ",,,,,,,,\r\n"
    )
    review_path(run_dir).write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))

    result = finalize(run_dir)

    assert result == FinalizeResult(applied=2, pending=0)
    assert stored(run_dir)["P1#1"][0] is Verdict.PASS
    assert stored(run_dir)["P2#1"] == (Verdict.FAIL, DecidedBy.HUMAN, "note")


def test_blank_verdict_stays_pending(tmp_path: Path) -> None:
    run_dir = queued_run(tmp_path)
    edit_review(run_dir, {"P1#1": ("pass", ""), "P2#1": ("", "undecided")})

    result = finalize(run_dir)

    assert result == FinalizeResult(applied=1, pending=1)
    assert stored(run_dir)["P2#1"][:2] == (Verdict.NEEDS_REVIEW, DecidedBy.NONE)
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "not reviewed" in report
    with (run_dir / "matrix.csv").open(encoding="utf-8-sig", newline="") as handle:
        matrix = {row["attempt_id"]: row for row in csv.DictReader(handle)}
    assert matrix["P2#1"]["verdict"] == "inconclusive"
    assert "not reviewed" in matrix["P2#1"]["reasons"]


def test_pending_counts_queued_attempts_missing_from_the_csv(tmp_path: Path) -> None:
    run_dir = queued_run(tmp_path)
    edit_review(run_dir, {"P1#1": ("pass", "")})
    # The reviewer deleted P2#1's row altogether: it is still waiting.
    with review_path(run_dir).open(encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row["attempt_id"] != "P2#1"]
    with review_path(run_dir).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(REVIEW_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)

    assert finalize(run_dir) == FinalizeResult(applied=1, pending=1)


def test_refinalize_overwrites_human_verdict(tmp_path: Path) -> None:
    run_dir = queued_run(tmp_path)
    edit_review(run_dir, {"P1#1": ("pass", "first"), "P2#1": ("pass", "")})
    finalize(run_dir)

    edit_review(run_dir, {"P1#1": ("fail", "second thoughts")})
    result = finalize(run_dir)

    assert result == FinalizeResult(applied=2, pending=0)
    assert stored(run_dir)["P1#1"] == (Verdict.FAIL, DecidedBy.HUMAN, "second thoughts")
    assert "FINAL" in (run_dir / "report.html").read_text(encoding="utf-8")


def test_blank_verdict_on_refinalize_keeps_the_earlier_human_verdict(tmp_path: Path) -> None:
    run_dir = queued_run(tmp_path)
    edit_review(run_dir, {"P1#1": ("pass", "kept"), "P2#1": ("fail", "")})
    finalize(run_dir)

    edit_review(run_dir, {"P1#1": ("", "")})
    result = finalize(run_dir)

    assert result == FinalizeResult(applied=1, pending=0)
    assert stored(run_dir)["P1#1"] == (Verdict.PASS, DecidedBy.HUMAN, "kept")


def test_finalize_does_not_touch_review_csv(tmp_path: Path) -> None:
    run_dir = queued_run(tmp_path)
    edit_review(run_dir, {"P1#1": ("pass", "ok"), "P2#1": ("fail", "")})
    before = review_path(run_dir).read_bytes()

    finalize(run_dir)

    assert review_path(run_dir).read_bytes() == before


def test_large_reply_in_review_csv_is_readable(tmp_path: Path) -> None:
    # csv's default 128 KB field limit would make a file our own report wrote unreadable.
    big = "x" * 300_000
    run_dir = make_run_dir(tmp_path, [("P1#1", "LLM01", Verdict.NEEDS_REVIEW, big)])
    write_reports(run_dir, final=False)
    # Fill the verdict by hand: the test's own csv reader would hit the same limit.
    text = review_path(run_dir).read_text(encoding="utf-8-sig", newline="")
    assert text.endswith(",," + CRLF)
    review_path(run_dir).write_text(text[:-4] + ",pass," + CRLF, encoding="utf-8-sig", newline="")

    limit_before = csv.field_size_limit()
    assert finalize(run_dir) == FinalizeResult(applied=1, pending=0)
    assert csv.field_size_limit() == limit_before  # the raised limit does not leak


# --- cell clean-up -----------------------------------------------------------------------------


def test_neutralised_cells_are_restored(tmp_path: Path) -> None:
    run_dir = queued_run(tmp_path)
    edit_review(run_dir, {"P1#1": ("'pass", "'=SUM(A1)"), "P2#1": ("fail", "'tis a leak")})

    finalize(run_dir)

    stored_rows = stored(run_dir)
    assert stored_rows["P1#1"] == (Verdict.PASS, DecidedBy.HUMAN, "=SUM(A1)")
    # An apostrophe that was not put there by neutralisation is part of what the person typed.
    assert stored_rows["P2#1"] == (Verdict.FAIL, DecidedBy.HUMAN, "'tis a leak")


def test_note_is_trimmed(tmp_path: Path) -> None:
    run_dir = queued_run(tmp_path)
    edit_review(run_dir, {"P1#1": ("pass", "  spaced out \t"), "P2#1": ("pass", "")})

    finalize(run_dir)

    assert stored(run_dir)["P1#1"][2] == "spaced out"


# --- validation: collect every problem, apply nothing ------------------------------------------


def test_invalid_rows_apply_nothing(tmp_path: Path) -> None:
    run_dir = queued_run(tmp_path)
    edit_review(
        run_dir,
        {"P1#1": ("pass", "fine"), "P2#1": ("maybe", "")},
        extra=[extra_row("ZZ#1", "pass")],
    )
    before = stored(run_dir)
    run_json_before = read_run_json(run_dir)
    report_before = (run_dir / "report.html").read_bytes()

    with pytest.raises(ConfigError) as caught:
        finalize(run_dir)

    lines = problem_lines(caught)
    assert len(lines) == 2
    assert lines[0].startswith("row 3 (attempt P2#1): ")
    assert "maybe" in lines[0]
    assert lines[1].startswith("row 4 (attempt ZZ#1): ")
    assert "unknown attempt" in lines[1]
    assert "row 2" not in str(caught.value)
    assert stored(run_dir) == before  # the valid row 2 was not applied either
    assert read_run_json(run_dir) == run_json_before  # not marked finalized
    assert (run_dir / "report.html").read_bytes() == report_before  # still the draft


def test_rule_decided_attempt_rejected(tmp_path: Path) -> None:
    rows = [
        ("P1#1", "LLM01", Verdict.NEEDS_REVIEW, "reply one"),
        ("P2#1", "LLM01", Verdict.PASS, "rule passed this"),
    ]
    run_dir = make_run_dir(tmp_path, rows)
    write_reports(run_dir, final=False)
    edit_review(run_dir, {"P1#1": ("pass", "")}, extra=[extra_row("P2#1", "fail")])
    before = stored(run_dir)

    with pytest.raises(ConfigError, match="decided by rule") as caught:
        finalize(run_dir)

    (line,) = problem_lines(caught)
    assert line.startswith("row 3 (attempt P2#1): ")
    assert stored(run_dir) == before


def test_attempt_that_was_never_queued_is_rejected(tmp_path: Path) -> None:
    rows = [
        ("P1#1", "LLM01", Verdict.NEEDS_REVIEW, "reply one"),
        ("P2#1", "LLM01", Verdict.INCONCLUSIVE, None),
    ]
    run_dir = make_run_dir(tmp_path, rows)
    write_reports(run_dir, final=False)
    edit_review(run_dir, {"P1#1": ("pass", "")}, extra=[extra_row("P2#1", "pass")])

    with pytest.raises(ConfigError, match="not queued for review") as caught:
        finalize(run_dir)

    assert "inconclusive" in str(caught.value)
    assert stored(run_dir)["P1#1"][0] is Verdict.NEEDS_REVIEW


def test_every_problem_is_listed_in_row_order(tmp_path: Path) -> None:
    rows = [
        ("P1#1", "LLM01", Verdict.NEEDS_REVIEW, "reply one"),
        ("P2#1", "LLM01", Verdict.FAIL, "rule failed this"),
    ]
    run_dir = make_run_dir(tmp_path, rows)
    write_reports(run_dir, final=False)
    edit_review(
        run_dir,
        {"P1#1": ("nope", "")},
        extra=[extra_row("P2#1", "pass"), extra_row("ZZ#9", "fail"), extra_row("", "pass")],
    )

    with pytest.raises(ConfigError) as caught:
        finalize(run_dir)

    lines = problem_lines(caught)
    assert [line.split(" ")[1] for line in lines] == ["2", "3", "4", "5"]
    assert "bad verdict" in lines[0]
    assert "decided by rule" in lines[1]
    assert "unknown attempt" in lines[2]
    assert lines[3].startswith("row 5 (attempt (blank)): ")
    assert "4 problems" in str(caught.value)


def test_conflicting_duplicate_rows_are_both_reported(tmp_path: Path) -> None:
    run_dir = queued_run(tmp_path)
    edit_review(
        run_dir,
        {"P1#1": ("pass", ""), "P2#1": ("pass", "")},
        extra=[extra_row("P1#1", "fail")],
    )

    with pytest.raises(ConfigError) as caught:
        finalize(run_dir)

    lines = problem_lines(caught)
    assert len(lines) == 2
    assert lines[0].startswith("row 2 (attempt P1#1): ") and "row 4" in lines[0]
    assert lines[1].startswith("row 4 (attempt P1#1): ") and "row 2" in lines[1]
    assert stored(run_dir)["P1#1"][0] is Verdict.NEEDS_REVIEW


def test_duplicate_rows_with_the_same_verdict_are_fine(tmp_path: Path) -> None:
    run_dir = queued_run(tmp_path)
    edit_review(
        run_dir,
        {"P1#1": ("pass", "first note"), "P2#1": ("pass", "")},
        extra=[extra_row("P1#1", "PASS", "")],
    )

    result = finalize(run_dir)

    assert result == FinalizeResult(applied=2, pending=0)
    assert stored(run_dir)["P1#1"] == (Verdict.PASS, DecidedBy.HUMAN, "first note")


def test_verdict_without_attempt_id_is_an_error(tmp_path: Path) -> None:
    run_dir = queued_run(tmp_path)
    edit_review(run_dir, {"P1#1": ("pass", "")}, extra=[extra_row("  ", "fail")])

    with pytest.raises(ConfigError, match="attempt_id is empty"):
        finalize(run_dir)


def test_short_rows_are_read_as_blank_cells(tmp_path: Path) -> None:
    run_dir = queued_run(tmp_path)
    text = ",".join(REVIEW_COLUMNS) + "\r\nP1#1,P1,LLM01,,,prompt,reply one,pass\r\nP2#1\r\n"
    review_path(run_dir).write_text(text, encoding="utf-8-sig", newline="")

    assert finalize(run_dir) == FinalizeResult(applied=1, pending=1)


# --- the files finalize needs ------------------------------------------------------------------


def test_missing_review_csv(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path, QUEUED)

    with pytest.raises(ConfigError, match="review.csv not found in") as caught:
        finalize(run_dir)

    assert str(run_dir) in str(caught.value)
    assert "finalized_at" not in read_run_json(run_dir)


@pytest.mark.parametrize("dropped", ["attempt_id", "verdict", "note"])
def test_missing_column_is_an_error(tmp_path: Path, dropped: str) -> None:
    run_dir = queued_run(tmp_path)
    header = [name for name in REVIEW_COLUMNS if name != dropped]
    review_path(run_dir).write_text(",".join(header) + "\r\n", encoding="utf-8-sig", newline="")

    with pytest.raises(ConfigError, match=dropped):
        finalize(run_dir)


def test_empty_review_csv(tmp_path: Path) -> None:
    run_dir = queued_run(tmp_path)
    review_path(run_dir).write_bytes(b"")

    with pytest.raises(ConfigError, match="review.csv is empty"):
        finalize(run_dir)


def test_missing_evidence_db_is_an_error_and_creates_none(tmp_path: Path) -> None:
    run_dir = queued_run(tmp_path)
    (run_dir / "evidence.db").unlink()

    with pytest.raises(ConfigError, match="evidence.db"):
        finalize(run_dir)

    assert not (run_dir / "evidence.db").exists()


def test_missing_run_json_is_an_error(tmp_path: Path) -> None:
    run_dir = queued_run(tmp_path)
    (run_dir / "run.json").unlink()

    with pytest.raises(ConfigError, match="run.json"):
        finalize(run_dir)


# --- writing the final reports -----------------------------------------------------------------


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can write a read-only file"
)
def test_a_report_file_that_cannot_be_written_is_a_config_error(tmp_path: Path) -> None:
    # Excel holds an open CSV locked on Windows; a read-only file fails the same way
    # (PermissionError) on every platform, without needing Excel.
    run_dir = queued_run(tmp_path)
    edit_review(run_dir, {"P1#1": ("pass", "")})
    matrix = run_dir / "matrix.csv"
    matrix.chmod(stat.S_IREAD)
    try:
        with pytest.raises(ConfigError) as caught:
            finalize(run_dir)
    finally:
        matrix.chmod(stat.S_IREAD | stat.S_IWRITE)

    assert str(caught.value) == (
        f"cannot write {matrix} — close it (e.g. in Excel) and run finalize again"
    )
    assert caught.value.__cause__ is None
    assert "finalized_at" not in read_run_json(run_dir)  # not finalized until the reports are

    # Once the file is closed, finalizing again completes the job.
    assert finalize(run_dir) == FinalizeResult(applied=1, pending=1)
    assert read_run_json(run_dir)["finalized_at"]


def test_finalized_at_is_stamped_only_after_the_final_reports_are_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_reports(run_dir: Path, *, final: bool) -> list[Path]:
        raise OSError("disk full")

    monkeypatch.setattr(review_module, "write_reports", broken_reports)
    run_dir = queued_run(tmp_path)
    edit_review(run_dir, {"P1#1": ("pass", "")})

    with pytest.raises(OSError, match="disk full"):
        finalize(run_dir)

    assert "finalized_at" not in read_run_json(run_dir)
