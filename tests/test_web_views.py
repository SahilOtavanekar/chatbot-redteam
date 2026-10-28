"""Tests for the HTML views (HTMX + Jinja2) over the service, on a fixture ``results/``."""

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from suite.models import Verdict
from suite.prompts import OWASP_LLM_2025
from suite.report import write_reports
from suite.web import service
from suite.web.app import create_app
from suite.web.settings import WebSettings
from tests.factories import make_run_dir

WEB_DIR = Path(service.__file__).parent


def _client(results_dir: Path) -> TestClient:
    return TestClient(create_app(WebSettings(results_dir=results_dir)))


def _queued_run(root: Path, reply: str = "maybe") -> Path:
    """A run with one rule pass and one attempt queued for review, with its draft ``review.csv``."""
    run = make_run_dir(
        root,
        [
            ("P-1#1", "LLM07", Verdict.PASS, "ok"),
            ("P-2#1", "LLM06", Verdict.NEEDS_REVIEW, reply),
        ],
    )
    write_reports(run, final=False)
    return run


def _verdict_url(run: Path, attempt_id: str = "P-2#1") -> str:
    return f"/runs/{run.name}/review/{attempt_id.replace('#', '%23')}"


# --- the runs page -----------------------------------------------------------------------------


def test_runs_page_lists_run(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    body = _client(tmp_path).get("/").text
    assert run.name in body


def test_runs_page_links_each_readable_run_to_its_report(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    r = _client(tmp_path).get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert f'href="/runs/{run.name}"' in r.text


def test_runs_page_says_so_when_there_are_no_runs(tmp_path):
    assert "No runs yet" in _client(tmp_path).get("/").text


def test_runs_page_lists_an_unreadable_run_without_a_link(tmp_path):
    (tmp_path / "20261023-000000-broken").mkdir()
    body = _client(tmp_path).get("/").text
    assert "20261023-000000-broken" in body and "unreadable" in body
    assert 'href="/runs/20261023-000000-broken"' not in body


def test_runs_page_offers_a_compare_form(tmp_path):
    make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.FAIL, "leak")])
    body = _client(tmp_path).get("/").text
    assert 'action="/compare"' in body and 'name="baseline"' in body and 'name="run"' in body


# --- the run (report) page ---------------------------------------------------------------------


def test_run_page_shows_totals_categories_and_prompts(tmp_path):
    run = make_run_dir(
        tmp_path,
        [("P-1#1", "LLM07", Verdict.PASS, "ok"), ("P-2#1", "LLM06", Verdict.FAIL, "leaked")],
    )
    r = _client(tmp_path).get(f"/runs/{run.name}")
    assert r.status_code == 200
    body = r.text
    assert "By OWASP" in body
    assert "LLM07" in body and "LLM06" in body
    assert "P-1" in body and "P-2" in body
    assert "Pass (rule)" in body and "Fail" in body
    assert service.LEGEND in body
    assert f'href="/runs/{run.name}/coverage"' in body


def test_run_page_for_a_draft_run_says_how_many_attempts_await_review(tmp_path):
    run = _queued_run(tmp_path)
    body = _client(tmp_path).get(f"/runs/{run.name}").text
    assert "DRAFT" in body and "1 attempts awaiting review" in body
    assert f'href="/runs/{run.name}/review"' in body


def test_run_page_for_a_finalized_run_is_final(tmp_path):
    run = _queued_run(tmp_path)
    c = _client(tmp_path)
    c.post(_verdict_url(run), data={"verdict": "pass", "note": ""})
    assert service.finalize_run(WebSettings(results_dir=tmp_path), run.name).ok
    body = c.get(f"/runs/{run.name}").text
    assert "FINAL" in body and "DRAFT" not in body


def test_run_page_is_404_for_an_unknown_run(tmp_path):
    assert _client(tmp_path).get("/runs/no-such-run").status_code == 404


def test_run_page_renders_a_reply_inert(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.PASS, "<script>x()</script>")])
    body = _client(tmp_path).get(f"/runs/{run.name}").text
    assert "<script>x()" not in body and "&lt;script&gt;" in body


# --- the coverage page -------------------------------------------------------------------------


def test_coverage_page_shows_all_categories(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    body = _client(tmp_path).get(f"/runs/{run.name}/coverage").text
    assert "LLM07" in body and "LLM01" in body


def test_coverage_page_lists_every_owasp_category_and_a_legend(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    body = _client(tmp_path).get(f"/runs/{run.name}/coverage").text
    for code, name in OWASP_LLM_2025.items():
        assert code in body and name in body
    assert "gap in coverage" in body
    assert service.LEGEND in body


def test_coverage_page_is_404_for_an_unknown_run(tmp_path):
    assert _client(tmp_path).get("/runs/no-such-run/coverage").status_code == 404


# --- the review queue page ---------------------------------------------------------------------


def test_reply_with_script_is_escaped(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM01", Verdict.NEEDS_REVIEW, "<script>x()</script>")])
    write_reports(run, final=False)
    body = _client(tmp_path).get(f"/runs/{run.name}/review").text
    assert "<script>x()" not in body and "&lt;script&gt;" in body


def test_review_page_shows_pass_if_the_full_reply_and_the_verdict_form(tmp_path):
    long_reply = "word " * 200 + "THE-END"
    run = _queued_run(tmp_path, reply=long_reply)
    r = _client(tmp_path).get(f"/runs/{run.name}/review")
    assert r.status_code == 200
    body = r.text
    assert "The bot answers safely" in body  # pass_if
    assert "THE-END" in body  # the reply is not cut short
    assert "P-2#1" in body and "P-1#1" not in body  # only the queued attempt
    assert f'hx-post="/runs/{run.name}/review/P-2%231"' in body
    assert 'name="verdict" value="pass"' in body and 'name="verdict" value="fail"' in body
    assert 'name="note"' in body
    assert f'hx-post="/runs/{run.name}/finalize"' in body


def test_review_page_with_an_empty_queue_says_so(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    write_reports(run, final=False)
    assert "Nothing to review" in _client(tmp_path).get(f"/runs/{run.name}/review").text


def test_review_page_shows_an_unreadable_review_csv_instead_of_a_500(tmp_path):
    run = _queued_run(tmp_path)
    (run / "review.csv").write_bytes(b"attempt_id,reply\n\xff\xfe\xfa,broken\n")
    r = _client(tmp_path).get(f"/runs/{run.name}/review")
    assert r.status_code == 200
    assert "review.csv" in r.text and "UTF-8" in r.text


def test_review_page_shows_a_config_error_from_the_service(tmp_path, monkeypatch):
    run = _queued_run(tmp_path)

    def locked(settings: WebSettings, run_id: str) -> list[service.ReviewItem]:
        raise service.ConfigError("review.csv: cannot read file (PermissionError); is it open?")

    monkeypatch.setattr(service, "review_queue", locked)
    r = _client(tmp_path).get(f"/runs/{run.name}/review")
    assert r.status_code == 200 and "cannot read file (PermissionError)" in r.text


def test_review_page_is_404_for_an_unknown_run(tmp_path):
    assert _client(tmp_path).get("/runs/no-such-run/review").status_code == 404


# --- saving a verdict --------------------------------------------------------------------------


def test_review_post_returns_the_item_with_the_chosen_verdict(tmp_path):
    run = _queued_run(tmp_path)
    r = _client(tmp_path).post(_verdict_url(run), data={"verdict": "pass", "note": "looks fine"})
    assert r.status_code == 200
    assert "<html" not in r.text  # a fragment, not a page
    assert "looks fine" in r.text and "P-2#1" in r.text
    assert re.search(r'value="pass"\s+aria-pressed="true"', r.text)
    assert re.search(r'value="fail"\s+aria-pressed="false"', r.text)
    assert "Saved: pass" in r.text
    (item,) = [i for i in service.review_queue(WebSettings(results_dir=tmp_path), run.name) or []]
    assert (item.verdict, item.note) == ("pass", "looks fine")


def test_review_post_clear_removes_the_verdict(tmp_path):
    run = _queued_run(tmp_path)
    c = _client(tmp_path)
    c.post(_verdict_url(run), data={"verdict": "fail", "note": "n"})
    r = c.post(_verdict_url(run), data={"verdict": "clear", "note": "n"})
    assert r.status_code == 200
    assert not re.search(r'aria-pressed="true"', r.text) and "Saved: no verdict" in r.text
    (item,) = service.review_queue(WebSettings(results_dir=tmp_path), run.name) or []
    assert item.verdict == ""


def test_review_page_clear_button_posts_the_word_the_view_understands(tmp_path):
    run = _queued_run(tmp_path)
    body = _client(tmp_path).get(f"/runs/{run.name}/review").text
    assert 'name="verdict" value="clear"' in body  # an empty value would be refused as missing


def test_review_post_without_a_verdict_is_refused(tmp_path):
    run = _queued_run(tmp_path)
    r = _client(tmp_path).post(_verdict_url(run), data={"note": "x"})
    assert r.status_code == 422  # never read as "clear"
    (item,) = service.review_queue(WebSettings(results_dir=tmp_path), run.name) or []
    assert item.verdict == ""


def test_review_post_renders_the_note_inert(tmp_path):
    run = _queued_run(tmp_path)
    r = _client(tmp_path).post(
        _verdict_url(run), data={"verdict": "fail", "note": "<script>x()</script>"}
    )
    assert "<script>x()" not in r.text and "&lt;script&gt;" in r.text


@pytest.mark.parametrize(
    ("attempt_id", "verdict", "message"),
    [
        ("P-2#1", "maybe", "verdict must be pass, fail or blank"),
        ("P-9#1", "pass", "attempt not found"),
    ],
)
def test_review_post_shows_a_refused_write_as_an_error_not_a_server_fault(
    tmp_path, attempt_id, verdict, message
):
    run = _queued_run(tmp_path)
    before = (run / "review.csv").read_bytes()
    r = _client(tmp_path).post(
        _verdict_url(run, attempt_id), data={"verdict": verdict, "note": "my typed note"}
    )
    assert r.status_code == 200  # HTMX only swaps a 2xx, so the error must arrive as one
    assert message in r.text and attempt_id in r.text
    assert "my typed note" in r.text  # kept in the form, so the reviewer can retry in place
    assert 'name="verdict" value="pass"' in r.text
    assert (run / "review.csv").read_bytes() == before


def test_review_post_for_an_unknown_run_shows_the_error(tmp_path):
    r = _client(tmp_path).post("/runs/no-such-run/review/P-2%231", data={"verdict": "pass"})
    assert r.status_code == 200 and "run not found" in r.text


# --- finalizing --------------------------------------------------------------------------------


def test_finalize_post_returns_a_status_with_the_counts(tmp_path):
    run = _queued_run(tmp_path)
    c = _client(tmp_path)
    c.post(_verdict_url(run), data={"verdict": "pass", "note": ""})
    r = c.post(f"/runs/{run.name}/finalize")
    assert r.status_code == 200
    assert "<html" not in r.text
    assert "Finalized" in r.text and "1 verdict" in r.text and "0 still pending" in r.text
    assert f'href="/runs/{run.name}"' in r.text


def test_finalize_post_shows_the_engine_error_instead_of_failing(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])  # no review.csv
    r = _client(tmp_path).post(f"/runs/{run.name}/finalize")
    assert r.status_code == 200
    assert "Could not finalize" in r.text and "review.csv" in r.text


def test_finalize_post_for_an_unknown_run_shows_the_error(tmp_path):
    r = _client(tmp_path).post("/runs/no-such-run/finalize")
    assert r.status_code == 200 and "run not found" in r.text


# --- compare -----------------------------------------------------------------------------------


def test_compare_page_shows_what_changed(tmp_path):
    baseline = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.FAIL, "leaked")])
    current = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    r = _client(tmp_path).get("/compare", params={"baseline": baseline.name, "run": current.name})
    assert r.status_code == 200
    assert "P-1" in r.text and "fixed" in r.text
    assert baseline.name in r.text and current.name in r.text


def test_compare_page_shows_a_dash_for_a_prompt_one_side_lacks(tmp_path):
    baseline = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    current = make_run_dir(
        tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok"), ("P-2#1", "LLM06", Verdict.PASS, "ok")]
    )
    body = (
        _client(tmp_path)
        .get("/compare", params={"baseline": baseline.name, "run": current.name})
        .text
    )
    assert "new" in body and service.NO_VERDICT in body


@pytest.mark.parametrize("same", [True, False])
def test_compare_page_is_404_for_the_same_or_an_unknown_run(tmp_path, same):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    other = run.name if same else "no-such-run"
    r = _client(tmp_path).get("/compare", params={"baseline": run.name, "run": other})
    assert r.status_code == 404


# --- static assets and template hygiene --------------------------------------------------------


def test_pages_load_htmx_and_the_stylesheet_from_this_server_only(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    c = _client(tmp_path)
    for path in ("/", f"/runs/{run.name}", f"/runs/{run.name}/coverage"):
        body = c.get(path).text
        assert '<script src="/static/htmx.min.js"' in body
        assert 'href="/static/app.css"' in body
        assert not re.search(r"""(?:src|href)=["']https?://""", body)


def test_vendored_htmx_is_the_pinned_release_and_is_served(tmp_path):
    r = _client(tmp_path).get("/static/htmx.min.js")
    assert r.status_code == 200 and len(r.content) > 40_000
    assert 'version:"1.9.12"' in r.text
    assert _client(tmp_path).get("/static/app.css").status_code == 200


def test_no_template_turns_escaping_off():
    # A bot reply is untrusted data: nothing may be marked safe, and autoescape stays on.
    templates = sorted((WEB_DIR / "templates").glob("*.html"))
    assert templates  # a wrong path must not pass with nothing to check
    for path in templates:
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"\|\s*safe\b|autoescape|Markup|\{%-?\s*raw", source), path.name
