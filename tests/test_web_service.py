"""Tests for the web layer's settings, run discovery, review queue and write-back."""

import codecs
import csv
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from suite.evidence import EvidenceStore, read_run_json, write_run_json
from suite.models import ConfigError, DecidedBy, Verdict
from suite.prompts import OWASP_LLM_2025
from suite.report import REVIEW_COLUMNS, write_reports
from suite.review import FinalizeResult
from suite.web import service
from suite.web.service import (
    compare,
    coverage,
    finalize_run,
    get_run,
    list_runs,
    resolve_run_dir,
    review_queue,
    set_review_verdict,
)
from suite.web.settings import WebSettings
from tests.factories import make_run_dir


def test_web_settings_defaults_to_loopback(tmp_path):
    s = WebSettings(results_dir=tmp_path)
    assert (s.host, s.port) == ("127.0.0.1", 8787)


def test_list_runs_reports_status_and_totals(tmp_path):
    make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    runs = list_runs(WebSettings(results_dir=tmp_path))
    assert len(runs) == 1 and runs[0].status == "complete"
    assert runs[0].totals["pass"] == 1 and runs[0].readable


def test_list_runs_marks_unreadable_folder(tmp_path):
    (tmp_path / "not-a-run").mkdir()
    runs = list_runs(WebSettings(results_dir=tmp_path))
    assert [r.readable for r in runs] == [False]  # listed, not raised


def test_resolve_run_dir_rejects_traversal(tmp_path):
    s = WebSettings(results_dir=tmp_path)
    (tmp_path / "real").mkdir()
    assert resolve_run_dir(s, "real") == tmp_path / "real"
    assert resolve_run_dir(s, "../secret") is None
    assert resolve_run_dir(s, "missing") is None


@pytest.mark.parametrize("run_id", ["", ".", "..", "../real", "real/..", "real/sub", "/real"])
def test_resolve_run_dir_rejects_unsafe_ids(tmp_path, run_id):
    (tmp_path / "real").mkdir()
    assert resolve_run_dir(WebSettings(results_dir=tmp_path), run_id) is None


def test_resolve_run_dir_rejects_a_plain_file(tmp_path):
    (tmp_path / "notes.txt").write_text("x")
    assert resolve_run_dir(WebSettings(results_dir=tmp_path), "notes.txt") is None


def test_list_runs_is_empty_when_results_dir_is_missing(tmp_path):
    assert list_runs(WebSettings(results_dir=tmp_path / "nope")) == []


def test_list_runs_ignores_loose_files(tmp_path):
    (tmp_path / "notes.txt").write_text("x")
    assert list_runs(WebSettings(results_dir=tmp_path)) == []


def test_list_runs_marks_corrupt_run_json_unreadable(tmp_path):
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "run.json").write_text("{not json", encoding="utf-8")
    [run] = list_runs(WebSettings(results_dir=tmp_path))
    assert (run.run_id, run.readable, run.status) == ("broken", False, "unreadable")
    assert run.totals == {} and run.endpoint_name is None and run.started_at is None


def test_list_runs_reports_endpoint_and_started_at(tmp_path):
    make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    [run] = list_runs(WebSettings(results_dir=tmp_path))
    assert run.run_id == "20261023-140509-t" and run.endpoint_name == "t"
    assert run.started_at == "2026-10-23T14:05:09+00:00"


def test_list_runs_finalized_comes_from_finalized_at(tmp_path):
    plain = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    done = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    write_run_json(done, {**read_run_json(done), "finalized_at": "2026-10-24T09:00:00+00:00"})
    finalized = {r.run_id: r.finalized for r in list_runs(WebSettings(results_dir=tmp_path))}
    assert finalized == {plain.name: False, done.name: True}


def test_list_runs_is_newest_first_then_by_name(tmp_path):
    first = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    second = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    newest = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    write_run_json(newest, {**read_run_json(newest), "started_at": "2026-11-01T00:00:00+00:00"})
    (tmp_path / "broken").mkdir()
    ids = [r.run_id for r in list_runs(WebSettings(results_dir=tmp_path))]
    assert ids == [newest.name, first.name, second.name, "broken"]


def test_get_run_matches_build_report_data(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    detail = get_run(WebSettings(results_dir=tmp_path), run.name)
    assert detail.report.totals[Verdict.PASS] == 1
    assert ("Endpoint", "t") in detail.facts


def test_get_run_none_for_missing(tmp_path):
    assert get_run(WebSettings(results_dir=tmp_path), "nope") is None


def test_coverage_lists_all_ten_categories(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    rows = coverage(WebSettings(results_dir=tmp_path), run.name)
    assert [r.code for r in rows] == list(OWASP_LLM_2025)
    present = {r.code: r.present for r in rows}
    assert present["LLM07"] and not present["LLM01"]


def test_get_run_is_a_draft_until_finalized(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.NEEDS_REVIEW, "hmm")])
    settings = WebSettings(results_dir=tmp_path)
    draft = get_run(settings, run.name)
    assert not draft.report.final and draft.report.pending == 1
    write_run_json(run, {**read_run_json(run), "finalized_at": "2026-10-24T09:00:00+00:00"})
    final = get_run(settings, run.name)
    assert final.report.final and final.report.totals[Verdict.INCONCLUSIVE] == 1


def test_get_run_none_when_evidence_is_missing(tmp_path):
    (tmp_path / "bare").mkdir()
    (tmp_path / "bare" / "run.json").write_text("{}", encoding="utf-8")
    assert get_run(WebSettings(results_dir=tmp_path), "bare") is None
    assert coverage(WebSettings(results_dir=tmp_path), "bare") is None


def test_coverage_none_for_missing_run(tmp_path):
    assert coverage(WebSettings(results_dir=tmp_path), "../nope") is None


def test_coverage_counts_prompts_not_attempts(tmp_path):
    run = make_run_dir(
        tmp_path,
        [
            ("P-1#1", "LLM07", Verdict.PASS, "ok"),
            ("P-1#2", "LLM07", Verdict.PASS, "ok"),
            ("P-2#1", "LLM07", Verdict.FAIL, "leak"),
            ("P-3#1", "LLM05", Verdict.PASS, "ok"),
        ],
    )
    rows = {r.code: r for r in coverage(WebSettings(results_dir=tmp_path), run.name)}
    assert (rows["LLM07"].prompt_count, rows["LLM05"].prompt_count) == (2, 1)
    assert rows["LLM01"].prompt_count == 0 and rows["LLM07"].name == OWASP_LLM_2025["LLM07"]


def test_compare_counts_changes(tmp_path):
    base = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.FAIL, "leak")])
    cur = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    view = compare(WebSettings(results_dir=tmp_path), base.name, cur.name)
    assert view.counts["fixed"] == 1


def test_compare_none_for_missing_side(tmp_path):
    cur = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    assert compare(WebSettings(results_dir=tmp_path), "nope", cur.name) is None
    assert compare(WebSettings(results_dir=tmp_path), cur.name, "nope") is None


def test_compare_none_for_the_same_run(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    assert compare(WebSettings(results_dir=tmp_path), run.name, run.name) is None


def test_compare_none_for_unsafe_id(tmp_path):
    cur = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    assert compare(WebSettings(results_dir=tmp_path), "../" + cur.name, cur.name) is None


def test_compare_none_when_a_side_has_no_evidence(tmp_path):
    cur = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    (tmp_path / "bare").mkdir()
    (tmp_path / "bare" / "run.json").write_text("{}", encoding="utf-8")
    assert compare(WebSettings(results_dir=tmp_path), "bare", cur.name) is None


def test_compare_rows_and_every_change_is_counted(tmp_path):
    base = make_run_dir(
        tmp_path,
        [
            ("P-1#1", "LLM07", Verdict.FAIL, "leak"),
            ("P-2#1", "LLM07", Verdict.PASS, "ok"),
            ("P-3#1", "LLM05", Verdict.PASS, "ok"),
        ],
    )
    cur = make_run_dir(
        tmp_path,
        [
            ("P-1#1", "LLM07", Verdict.PASS, "ok"),
            ("P-2#1", "LLM07", Verdict.FAIL, "leak"),
            ("P-4#1", "LLM06", Verdict.PASS, "ok"),
        ],
    )
    view = compare(WebSettings(results_dir=tmp_path), base.name, cur.name)
    assert (view.baseline_id, view.run_id) == (base.name, cur.name)
    assert {row.prompt_id: row.change.value for row in view.rows} == {
        "P-1": "fixed",
        "P-2": "regressed",
        "P-4": "new",
        "P-3": "removed",
    }
    assert view.counts == {
        "fixed": 1,
        "regressed": 1,
        "unchanged": 0,
        "unclear": 0,
        "new": 1,
        "removed": 1,
    }


def test_review_queue_reads_full_reply(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "a long reply ...")])
    write_reports(run, final=False)  # writes review.csv
    items = review_queue(WebSettings(results_dir=tmp_path), run.name)
    assert len(items) == 1 and items[0].attempt_id == "P-1#1"
    assert items[0].reply == "a long reply ..." and items[0].verdict == ""


def test_review_queue_empty_without_file(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    assert review_queue(WebSettings(results_dir=tmp_path), run.name) == []


def test_review_queue_none_for_unknown_or_unsafe_run(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "x")])
    write_reports(run, final=False)
    settings = WebSettings(results_dir=tmp_path)
    assert review_queue(settings, "no-such-run") is None
    assert review_queue(settings, "../" + run.name) is None


def test_review_queue_keeps_replies_longer_than_the_default_csv_field_limit(tmp_path):
    long_reply = "x" * (csv.field_size_limit() + 1000)
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, long_reply)])
    write_reports(run, final=False)
    items = review_queue(WebSettings(results_dir=tmp_path), run.name)
    assert items is not None and items[0].reply == long_reply


def test_review_queue_restores_the_csv_field_limit(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "x")])
    write_reports(run, final=False)
    before = csv.field_size_limit()
    review_queue(WebSettings(results_dir=tmp_path), run.name)
    assert csv.field_size_limit() == before


def _cells(path):
    """The raw cells of a CSV file (a long reply needs the field limit lifted to be read)."""
    previous = csv.field_size_limit(2**31 - 1)
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.reader(handle))
    finally:
        csv.field_size_limit(previous)


def test_set_verdict_writes_back_and_finalize_applies(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "reply")])
    write_reports(run, final=False)
    s = WebSettings(results_dir=tmp_path)
    w = set_review_verdict(s, run.name, "P-1#1", "pass", "looks fine")
    assert w.ok and w.item.verdict == "pass"
    assert review_queue(s, run.name)[0].verdict == "pass"  # persisted
    out = finalize_run(s, run.name)
    assert out.ok and out.applied == 1


def test_set_verdict_neutralises_formula_note(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "reply")])
    write_reports(run, final=False)
    s = WebSettings(results_dir=tmp_path)
    set_review_verdict(s, run.name, "P-1#1", "fail", "=cmd()")
    assert review_queue(s, run.name)[0].note.startswith("'=")  # csv_cell guard


def test_set_verdict_rejects_bad_verdict_and_unknown_attempt(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "reply")])
    write_reports(run, final=False)
    s = WebSettings(results_dir=tmp_path)
    assert not set_review_verdict(s, run.name, "P-1#1", "maybe", "").ok
    assert not set_review_verdict(s, run.name, "ZZ#9", "pass", "").ok


def test_finalize_run_surfaces_config_error(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])  # no review.csv
    out = finalize_run(WebSettings(results_dir=tmp_path), run.name)
    assert not out.ok and "review.csv" in out.error  # nothing half-applied


def test_set_verdict_result_carries_the_updated_item(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "the reply")])
    write_reports(run, final=False)
    write = set_review_verdict(WebSettings(results_dir=tmp_path), run.name, "P-1#1", "fail", "n")
    assert write.error is None
    assert write.item.attempt_id == "P-1#1" and write.item.reply == "the reply"
    assert (write.item.verdict, write.item.note) == ("fail", "n")


def test_set_verdict_leaves_every_other_cell_verbatim(tmp_path):
    awkward = 'He said "hi", then\r\nleft\nlines — café 日本'
    run = make_run_dir(
        tmp_path,
        [
            ("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "=SUM(1,2)"),
            ("P-2#1", "LLM06", Verdict.NEEDS_REVIEW, awkward),
            ("P-3#1", "LLM01", Verdict.NEEDS_REVIEW, "x" * (csv.field_size_limit() + 5)),
        ],
    )
    write_reports(run, final=False)
    path = run / "review.csv"
    before = _cells(path)
    assert before[1][6] == "'=SUM(1,2)"  # the engine's own formula guard, already in the file
    s = WebSettings(results_dir=tmp_path)
    assert set_review_verdict(s, run.name, "P-2#1", "fail", "bad").ok
    after = _cells(path)
    assert path.read_bytes().startswith(codecs.BOM_UTF8)
    assert after[0] == list(REVIEW_COLUMNS) == before[0]
    assert len(after) == len(before)
    for old, new in zip(before[1:], after[1:], strict=True):
        if old[0] == "P-2#1":
            assert new[:7] == old[:7] and new[7:] == ["fail", "bad"]
        else:
            assert new == old  # nothing re-prefixed, re-quoted or truncated


def test_set_verdict_is_case_insensitive_and_blank_clears_it(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "reply")])
    write_reports(run, final=False)
    s = WebSettings(results_dir=tmp_path)
    assert set_review_verdict(s, run.name, "P-1#1", "PASS", "keep").item.verdict == "pass"
    assert set_review_verdict(s, run.name, "P-1#1", "Fail", "").item.verdict == "fail"
    cleared = set_review_verdict(s, run.name, "P-1#1", "", "")
    assert cleared.ok and (cleared.item.verdict, cleared.item.note) == ("", "")
    assert review_queue(s, run.name)[0].verdict == ""


def test_set_verdict_rejects_without_touching_the_file(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "reply")])
    write_reports(run, final=False)
    path = run / "review.csv"
    before = path.read_bytes()
    s = WebSettings(results_dir=tmp_path)
    for attempt_id, verdict in (("P-1#1", "maybe"), ("P-1#1", "pass "), ("ZZ#9", "pass")):
        write = set_review_verdict(s, run.name, attempt_id, verdict, "n")
        assert not write.ok and write.item is None and write.error
    assert path.read_bytes() == before


def test_set_verdict_rejects_a_blank_attempt_id_and_writes_nothing(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "reply")])
    write_reports(run, final=False)
    path = run / "review.csv"
    rows = _cells(path)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:  # a stray blank row
        csv.writer(handle).writerows([*rows, [""] * len(REVIEW_COLUMNS)])
    before = path.read_bytes()
    s = WebSettings(results_dir=tmp_path)
    assert review_queue(s, run.name)[1].attempt_id == ""  # the UI can be handed this row
    for attempt_id in ("", "   "):
        write = set_review_verdict(s, run.name, attempt_id, "pass", "x")
        assert not write.ok and write.item is None and write.error
    assert path.read_bytes() == before  # no phantom verdict that would block finalize
    assert finalize_run(s, run.name).ok


def test_set_verdict_accepts_the_padded_attempt_id_the_queue_returns(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "reply")])
    write_reports(run, final=False)
    path = run / "review.csv"
    rows = _cells(path)
    rows[1][0] = " P-1#1 "  # a hand-padded cell the engine still matches
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        csv.writer(handle).writerows(rows)
    s = WebSettings(results_dir=tmp_path)
    shown = review_queue(s, run.name)[0].attempt_id
    assert shown == " P-1#1 "
    assert set_review_verdict(s, run.name, shown, "pass", "").ok
    assert review_queue(s, run.name)[0].verdict == "pass"
    assert finalize_run(s, run.name).applied == 1


def test_set_verdict_fails_cleanly_for_unknown_run_or_missing_file(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])  # no review.csv
    s = WebSettings(results_dir=tmp_path)
    for run_id in ("no-such-run", "../" + run.name, run.name):
        write = set_review_verdict(s, run_id, "P-1#1", "pass", "")
        assert not write.ok and write.item is None and write.error
    assert not (run / "review.csv").exists()


def test_set_verdict_updates_every_row_listed_for_the_attempt(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "reply")])
    write_reports(run, final=False)
    path = run / "review.csv"
    rows = _cells(path)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:  # a hand-duplicated row
        csv.writer(handle).writerows([*rows, rows[1]])
    s = WebSettings(results_dir=tmp_path)
    assert set_review_verdict(s, run.name, "P-1#1", "pass", "").ok
    assert [item.verdict for item in review_queue(s, run.name)] == ["pass", "pass"]


def test_set_verdict_reads_a_sheet_whose_header_was_retyped(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "keep this reply")])
    write_reports(run, final=False)
    path = run / "review.csv"
    rows = _cells(path)
    rows[0] = [f" {name.upper()} " for name in rows[0]]  # the engine accepts this header
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        csv.writer(handle).writerows(rows)
    s = WebSettings(results_dir=tmp_path)
    assert set_review_verdict(s, run.name, "P-1#1", "pass", "n").ok
    assert _cells(path)[0] == list(REVIEW_COLUMNS)
    item = review_queue(s, run.name)[0]
    assert (item.reply, item.verdict) == ("keep this reply", "pass")  # nothing blanked


def test_set_verdict_reports_an_unreadable_file_and_leaves_it_alone(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "reply")])
    write_reports(run, final=False)
    path = run / "review.csv"
    path.write_bytes(path.read_bytes() + b"\xff\xfe not utf-8 \xff")
    before = path.read_bytes()
    limit = csv.field_size_limit()
    s = WebSettings(results_dir=tmp_path)
    write = set_review_verdict(s, run.name, "P-1#1", "pass", "")
    assert not write.ok and write.item is None and "review.csv" in write.error
    assert path.read_bytes() == before  # never rewritten with replacement characters
    assert csv.field_size_limit() == limit  # restored even though the read failed
    with pytest.raises(ConfigError):
        review_queue(s, run.name)


def test_set_verdict_keeps_the_original_when_the_write_fails(tmp_path, monkeypatch):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "reply")])
    write_reports(run, final=False)
    path = run / "review.csv"
    before = path.read_bytes()

    def locked(src, dst):  # what Windows does when the file is open in Excel
        raise PermissionError(13, "in use", str(dst))

    monkeypatch.setattr("suite.web.service.os.replace", locked)
    write = set_review_verdict(WebSettings(results_dir=tmp_path), run.name, "P-1#1", "pass", "")
    assert not write.ok and write.item is None and "review.csv" in write.error
    assert path.read_bytes() == before
    assert sorted(entry.name for entry in run.glob("review.csv*")) == ["review.csv"]  # no temp


def test_concurrent_verdicts_are_not_lost(tmp_path):
    count = 12
    run = make_run_dir(
        tmp_path,
        [(f"P-{i}#1", "LLM01", Verdict.NEEDS_REVIEW, f"reply {i}") for i in range(1, count + 1)],
    )
    write_reports(run, final=False)
    s = WebSettings(results_dir=tmp_path)
    start = threading.Barrier(count * 2)

    def write(i):
        start.wait()
        return set_review_verdict(s, run.name, f"P-{i}#1", "pass", f"note {i}")

    def read(_):
        start.wait()
        return review_queue(s, run.name)

    with ThreadPoolExecutor(max_workers=count * 2) as pool:
        writes = pool.map(write, range(1, count + 1))
        reads = pool.map(read, range(count))
        assert all(w.ok for w in writes), "a verdict write failed under contention"
        assert all(len(rows) == count for rows in reads)  # a read never sees a torn file
    items = review_queue(s, run.name)
    assert [(item.verdict, item.note) for item in items] == [
        ("pass", f"note {i}") for i in range(1, count + 1)
    ]


def test_finalize_records_the_human_verdict_and_the_note_as_typed(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "reply")])
    write_reports(run, final=False)
    s = WebSettings(results_dir=tmp_path)
    assert set_review_verdict(s, run.name, "P-1#1", "fail", "=cmd()").ok
    assert finalize_run(s, run.name).ok
    store = EvidenceStore(run / "evidence.db")
    try:
        (attempt,) = store.attempts()
    finally:
        store.close()
    assert (attempt.verdict, attempt.decided_by) == (Verdict.FAIL, DecidedBy.HUMAN)
    assert attempt.human_note == "=cmd()"  # the engine strips the guard the sheet carried


def test_finalize_run_applies_nothing_when_a_row_is_bad(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "reply")])
    write_reports(run, final=False)
    path = run / "review.csv"
    rows = _cells(path)
    rows[1][7] = "maybe"  # a reviewer typed this into the sheet by hand
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        csv.writer(handle).writerows(rows)
    out = finalize_run(WebSettings(results_dir=tmp_path), run.name)
    assert not out.ok and out.applied is None and out.pending is None and "maybe" in out.error
    assert not read_run_json(run).get("finalized_at")


def test_finalize_run_counts_applied_and_pending(tmp_path):
    run = make_run_dir(
        tmp_path,
        [
            ("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "a"),
            ("P-2#1", "LLM01", Verdict.NEEDS_REVIEW, "b"),
        ],
    )
    write_reports(run, final=False)
    s = WebSettings(results_dir=tmp_path)
    set_review_verdict(s, run.name, "P-1#1", "fail", "")
    out = finalize_run(s, run.name)
    assert (out.ok, out.applied, out.pending, out.error) == (True, 1, 1, None)
    assert read_run_json(run).get("finalized_at")


def test_finalize_run_is_not_ok_for_an_unknown_run(tmp_path):
    out = finalize_run(WebSettings(results_dir=tmp_path), "no-such-run")
    assert not out.ok and out.applied is None and out.pending is None and out.error


def test_finalize_run_holds_the_review_lock(tmp_path, monkeypatch):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "reply")])
    held = []

    def fake_finalize(run_dir):
        def try_lock():  # from another thread: an RLock only refuses a non-owner
            got = service._review_csv_lock.acquire(blocking=False)
            held.append(not got)
            if got:
                service._review_csv_lock.release()

        worker = threading.Thread(target=try_lock)
        worker.start()
        worker.join()
        return FinalizeResult(applied=0, pending=0)

    monkeypatch.setattr("suite.web.service.finalize", fake_finalize)
    assert finalize_run(WebSettings(results_dir=tmp_path), run.name).ok
    assert held == [True]


def test_review_listing_returns_the_queue_without_an_error(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "reply")])
    write_reports(run, final=False)
    listing = service.review_listing(WebSettings(results_dir=tmp_path), run.name)
    assert listing is not None and listing.error is None
    assert [item.attempt_id for item in listing.items] == ["P-1#1"]


def test_review_listing_is_none_for_an_unknown_or_unsafe_run(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "x")])
    write_reports(run, final=False)
    settings = WebSettings(results_dir=tmp_path)
    assert service.review_listing(settings, "no-such-run") is None
    assert service.review_listing(settings, "../" + run.name) is None


def test_review_listing_reports_an_unreadable_review_csv_as_an_error(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "reply")])
    write_reports(run, final=False)
    (run / "review.csv").write_bytes(b"attempt_id,reply\n\xff\xfe\xfa,broken\n")
    listing = service.review_listing(WebSettings(results_dir=tmp_path), run.name)
    assert listing is not None and listing.items == []
    assert listing.error is not None and "review.csv" in listing.error and "UTF-8" in listing.error
