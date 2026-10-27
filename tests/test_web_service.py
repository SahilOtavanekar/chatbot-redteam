"""Tests for the web layer's settings, run discovery and path-safe run resolution."""

import pytest

from suite.evidence import read_run_json, write_run_json
from suite.models import Verdict
from suite.prompts import OWASP_LLM_2025
from suite.web.service import compare, coverage, get_run, list_runs, resolve_run_dir
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
