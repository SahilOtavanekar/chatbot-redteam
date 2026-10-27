"""Tests for the web app factory and its JSON API (``/api/v1``), over a fixture ``results/``."""

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from suite.config import load_endpoint, masked
from suite.evidence import read_run_json, write_run_json
from suite.models import ConfigError, DecidedBy, Verdict
from suite.prompts import OWASP_LLM_2025
from suite.report import REVIEW_COLUMNS, VERDICT_LEGEND, verdict_label, write_reports
from suite.web import service
from suite.web.app import create_app
from suite.web.settings import WebSettings
from tests.factories import make_run_dir

API = "/api/v1"


def _client(results_dir: Path) -> TestClient:
    return TestClient(create_app(WebSettings(results_dir=results_dir)))


def _queued_run(root: Path) -> Path:
    """A run with one rule pass and one attempt queued for review, with its draft ``review.csv``."""
    run = make_run_dir(
        root,
        [
            ("P-1#1", "LLM07", Verdict.PASS, "ok"),
            ("P-2#1", "LLM06", Verdict.NEEDS_REVIEW, "maybe"),
        ],
    )
    write_reports(run, final=False)
    return run


# --- the app factory ---------------------------------------------------------------------------


def test_create_app_stores_settings_and_serves_the_api(tmp_path):
    settings = WebSettings(results_dir=tmp_path)
    app = create_app(settings)
    assert isinstance(app, FastAPI)
    assert app.state.settings is settings
    assert TestClient(app).get(f"{API}/runs").status_code == 200


def test_create_app_mounts_static_without_serving_unknown_files(tmp_path):
    # The static folder is mounted (an unknown file is a 404 from it, not a crash at start-up).
    assert _client(tmp_path).get("/static/nope.css").status_code == 404


def test_api_publishes_an_openapi_schema_for_every_route(tmp_path):
    paths = _client(tmp_path).get("/openapi.json").json()["paths"]
    assert set(paths) == {
        f"{API}/runs",
        f"{API}/runs/{{run_id}}",
        f"{API}/runs/{{run_id}}/coverage",
        f"{API}/runs/{{run_id}}/review",
        f"{API}/runs/{{run_id}}/review/{{attempt_id}}",
        f"{API}/runs/{{run_id}}/finalize",
        f"{API}/compare",
    }


@pytest.mark.parametrize("module", ["api.py", "app.py", "deps.py", "views.py"])
def test_routers_reach_the_engine_only_through_the_service(module):
    source = (Path(service.__file__).parent / module).read_text(encoding="utf-8")
    # Catches ``from suite.x import ...``, ``import suite.x`` and the bare ``from suite import x``.
    engine_imports = re.findall(
        r"^\s*(?:(?:from|import)\s+suite\.(?!web\b)\w+|from\s+suite\s+import\b)", source, re.M
    )
    assert engine_imports == []


# --- runs --------------------------------------------------------------------------------------


def test_api_runs_and_detail(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    c = _client(tmp_path)
    assert c.get("/api/v1/runs").json()[0]["run_id"] == run.name
    assert c.get(f"/api/v1/runs/{run.name}").status_code == 200


def test_api_runs_lists_summaries(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    r = _client(tmp_path).get(f"{API}/runs")
    assert r.status_code == 200
    (summary,) = r.json()
    assert summary["run_id"] == run.name
    assert summary["status"] == "complete" and summary["readable"] is True
    assert summary["finalized"] is False
    assert summary["totals"]["pass"] == 1


def test_api_runs_is_an_empty_list_for_an_empty_or_missing_results_dir(tmp_path):
    assert _client(tmp_path).get(f"{API}/runs").json() == []
    assert _client(tmp_path / "never-created").get(f"{API}/runs").json() == []


def test_api_run_detail_carries_report_facts_and_legend(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    r = _client(tmp_path).get(f"{API}/runs/{run.name}")
    body = r.json()
    # The whole run.json is served, so the endpoint in it must already be masked: the factory's
    # endpoint carries a bearer token, and no secret may reach the JSON surface.
    assert "s3cret-token" not in r.text
    assert body["report"]["run"]["endpoint"]["headers"] == {"Authorization": "***"}
    assert body["run_id"] == run.name
    assert body["legend"] == VERDICT_LEGEND
    assert body["report"]["final"] is False
    assert body["report"]["totals"]["pass"] == 1
    assert body["report"]["prompts"][0]["prompt_id"] == "P-1"
    assert body["report"]["prompts"][0]["verdict"] == "pass"
    assert body["facts"]  # the header facts, as [label, value] pairs


def test_api_never_serves_a_value_substituted_for_an_env_variable(tmp_path):
    # Mirrors what ``suite run`` stores: ``masked(spec)`` of the loaded spec, whose url carries a
    # ${VAR} value. If a change ever wrote the endpoint unmasked, this secret would leak.
    secret = "k-9f3a-hunter2"
    endpoint_file = tmp_path / "endpoint.yaml"
    endpoint_file.write_text(
        "name: t\ntype: http\nauthorized: test\n"
        "url: http://127.0.0.1:9/chat?key=${API_KEY}\n"
        'body: {message: "{prompt}"}\n',
        encoding="utf-8",
    )
    spec = load_endpoint(endpoint_file, env={"API_KEY": secret})
    assert secret in spec.url  # the secret really is in the loaded spec
    results = tmp_path / "results"
    run = make_run_dir(results, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    record = read_run_json(run)
    record["endpoint"] = masked(spec)
    write_run_json(run, record)

    c = _client(results)
    for path in ("/runs", f"/runs/{run.name}", f"/runs/{run.name}/coverage"):
        assert secret not in c.get(f"{API}{path}").text, path
    body = c.get(f"{API}/runs/{run.name}").json()
    assert body["report"]["run"]["endpoint"]["url"] == "http://127.0.0.1:9/chat?key=***"


def test_api_run_coverage_has_one_row_per_owasp_category(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    r = _client(tmp_path).get(f"{API}/runs/{run.name}/coverage")
    assert r.status_code == 200
    rows = r.json()
    assert [row["code"] for row in rows] == list(OWASP_LLM_2025)
    by_code = {row["code"]: row for row in rows}
    assert by_code["LLM07"]["present"] is True and by_code["LLM07"]["prompt_count"] == 1
    assert by_code["LLM01"]["present"] is False


# --- unknown and unsafe ids are 404 -------------------------------------------------------------


def test_api_traversal_and_missing_are_404(tmp_path):
    c = _client(tmp_path)
    assert c.get("/api/v1/runs/..%2Fsecret").status_code == 404
    assert c.get("/api/v1/runs/nope").status_code == 404


@pytest.mark.parametrize(
    "run_id",
    [
        "..%2Fsecret",  # a sibling folder of results/ that really exists
        "..%2F..%2F..",
        "%2Fetc",
        "C%3A%5CWindows",
        "%5C..%5Csecret",
        "nope",
    ],
)
@pytest.mark.parametrize("suffix", ["", "/coverage", "/review"])
def test_api_run_routes_404_for_traversal_and_missing_ids(tmp_path, run_id, suffix):
    (tmp_path / "secret").mkdir()
    results = tmp_path / "results"
    make_run_dir(results, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    assert _client(results).get(f"{API}/runs/{run_id}{suffix}").status_code == 404


def test_api_a_file_in_results_is_not_a_run(tmp_path):
    (tmp_path / "notes.txt").write_text("x")
    c = _client(tmp_path)
    for suffix in ("", "/coverage", "/review"):
        assert c.get(f"{API}/runs/notes.txt{suffix}").status_code == 404


def test_api_run_without_evidence_is_404_but_still_listed(tmp_path):
    (tmp_path / "empty-run").mkdir()
    c = _client(tmp_path)
    assert c.get(f"{API}/runs/empty-run").status_code == 404
    assert c.get(f"{API}/runs/empty-run/coverage").status_code == 404
    (summary,) = c.get(f"{API}/runs").json()
    assert summary["run_id"] == "empty-run" and summary["readable"] is False


# --- review queue ------------------------------------------------------------------------------


def test_api_review_lists_the_queue(tmp_path):
    run = _queued_run(tmp_path)
    r = _client(tmp_path).get(f"{API}/runs/{run.name}/review")
    assert r.status_code == 200
    body = r.json()
    assert body["error"] is None
    (item,) = body["items"]
    assert item["attempt_id"] == "P-2#1" and item["verdict"] == "" and item["note"] == ""
    assert set(item) == set(REVIEW_COLUMNS)


def test_api_review_is_empty_without_a_review_csv(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    r = _client(tmp_path).get(f"{API}/runs/{run.name}/review")
    assert r.status_code == 200 and r.json() == {"items": [], "error": None}


def test_api_review_corrupt_csv_is_not_a_500_and_carries_the_message(tmp_path):
    run = _queued_run(tmp_path)
    (run / "review.csv").write_bytes(b"attempt_id,reply\n\xff\xfe\xfa,broken\n")
    r = _client(tmp_path).get(f"{API}/runs/{run.name}/review")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert "review.csv" in body["error"] and "UTF-8" in body["error"]


def test_api_review_service_config_error_becomes_an_error_payload(tmp_path, monkeypatch):
    run = _queued_run(tmp_path)

    def locked(settings: WebSettings, run_id: str) -> list[service.ReviewItem]:
        raise ConfigError("review.csv: cannot read file (PermissionError); is it open?")

    monkeypatch.setattr(service, "review_queue", locked)
    r = _client(tmp_path).get(f"{API}/runs/{run.name}/review")
    assert r.status_code == 200
    assert r.json() == {
        "items": [],
        "error": "review.csv: cannot read file (PermissionError); is it open?",
    }


# --- verdict write-back ------------------------------------------------------------------------


def test_api_put_verdict_saves_and_returns_the_item(tmp_path):
    run = _queued_run(tmp_path)
    c = _client(tmp_path)
    r = c.put(f"{API}/runs/{run.name}/review/P-2%231", json={"verdict": "Pass", "note": "fine"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["error"] is None
    assert body["item"]["attempt_id"] == "P-2#1"
    assert body["item"]["verdict"] == "pass" and body["item"]["note"] == "fine"
    (item,) = c.get(f"{API}/runs/{run.name}/review").json()["items"]
    assert item["verdict"] == "pass"


def test_api_put_verdict_note_is_optional(tmp_path):
    run = _queued_run(tmp_path)
    r = _client(tmp_path).put(f"{API}/runs/{run.name}/review/P-2%231", json={"verdict": "fail"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["item"]["note"] == ""


@pytest.mark.parametrize(
    ("run_id", "attempt_id", "verdict"),
    [
        ("{run}", "P-2%231", "maybe"),  # not pass / fail / blank
        ("{run}", "P-9%239", "pass"),  # attempt not in the file
        ("nope", "P-2%231", "pass"),  # unknown run
        ("%2E%2E", "P-2%231", "pass"),  # traversal: ".." reaches the service, which refuses it
        ("%5C..%5Csecret", "P-2%231", "pass"),
    ],
)
def test_api_put_verdict_failure_is_a_200_payload_not_an_error(
    tmp_path, run_id, attempt_id, verdict
):
    run = _queued_run(tmp_path)
    before = (run / "review.csv").read_bytes()
    r = _client(tmp_path).put(
        f"{API}/runs/{run_id.format(run=run.name)}/review/{attempt_id}",
        json={"verdict": verdict, "note": ""},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["item"] is None and body["error"]
    assert (run / "review.csv").read_bytes() == before


def test_api_put_verdict_on_a_run_with_no_review_csv_is_an_error_payload(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    r = _client(tmp_path).put(
        f"{API}/runs/{run.name}/review/P-1%231", json={"verdict": "pass", "note": ""}
    )
    assert r.status_code == 200 and r.json()["ok"] is False


def test_api_put_verdict_without_a_verdict_is_a_422(tmp_path):
    run = _queued_run(tmp_path)
    r = _client(tmp_path).put(f"{API}/runs/{run.name}/review/P-2%231", json={"note": "x"})
    assert r.status_code == 422


# --- finalize ----------------------------------------------------------------------------------


def test_api_finalize_error_is_200_payload_not_500(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    r = _client(tmp_path).post(f"/api/v1/runs/{run.name}/finalize")
    assert r.status_code == 200 and r.json()["ok"] is False


def test_api_finalize_error_payload_carries_the_message(tmp_path):
    run = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    body = _client(tmp_path).post(f"{API}/runs/{run.name}/finalize").json()
    assert body["ok"] is False
    assert body["error"] and body["applied"] is None and body["pending"] is None


@pytest.mark.parametrize("run_id", ["nope", "%2E%2E", "%5C..%5Csecret", "C%3A%5CWindows"])
def test_api_finalize_unknown_or_unsafe_run_is_a_200_error_payload(tmp_path, run_id):
    (tmp_path / "secret").mkdir()
    results = tmp_path / "results"
    results.mkdir()
    r = _client(results).post(f"{API}/runs/{run_id}/finalize")
    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["error"] == "run not found"


@pytest.mark.parametrize("verb", ["post", "put"])
def test_api_an_encoded_slash_in_a_run_id_never_reaches_a_write(tmp_path, verb):
    # The server decodes %2F before routing, so "../secret" is a different path with no route:
    # a 404 from the router, and the service is never called.
    run = _queued_run(tmp_path / "results")
    before = (run / "review.csv").read_bytes()
    c = _client(tmp_path / "results")
    if verb == "post":
        r = c.post(f"{API}/runs/..%2Fsecret/finalize")
    else:
        r = c.put(f"{API}/runs/..%2Fsecret/review/P-2%231", json={"verdict": "pass", "note": ""})
    assert r.status_code == 404
    assert (run / "review.csv").read_bytes() == before


def test_api_finalize_applies_the_saved_verdicts(tmp_path):
    run = _queued_run(tmp_path)
    c = _client(tmp_path)
    c.put(f"{API}/runs/{run.name}/review/P-2%231", json={"verdict": "pass", "note": "ok"})
    r = c.post(f"{API}/runs/{run.name}/finalize")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "applied": 1, "pending": 0, "error": None}
    summary = c.get(f"{API}/runs").json()[0]
    assert summary["finalized"] is True


def test_api_finalize_with_a_bad_row_is_an_error_payload_and_applies_nothing(tmp_path):
    run = _queued_run(tmp_path)
    # A verdict on an attempt a rule already decided: the engine refuses the whole sheet.
    (run / "review.csv").write_text(
        "attempt_id,verdict,note\nP-1#1,pass,\nP-2#1,pass,\n", encoding="utf-8", newline=""
    )
    c = _client(tmp_path)
    r = c.post(f"{API}/runs/{run.name}/finalize")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and "P-1#1" in body["error"]
    assert c.get(f"{API}/runs").json()[0]["finalized"] is False


# --- compare -----------------------------------------------------------------------------------


def test_api_compare_returns_rows_and_counts(tmp_path):
    base = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.FAIL, "leak")])
    now = make_run_dir(tmp_path, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    r = _client(tmp_path).get(f"{API}/compare", params={"baseline": base.name, "run": now.name})
    assert r.status_code == 200
    body = r.json()
    assert (body["baseline_id"], body["run_id"]) == (base.name, now.name)
    assert body["counts"]["fixed"] == 1 and body["counts"]["regressed"] == 0
    (row,) = body["rows"]
    assert row["prompt_id"] == "P-1" and row["change"] == "fixed"
    assert row["baseline"] == "fail" and row["current"] == "pass"


def test_api_compare_404_for_unknown_unsafe_or_identical_runs(tmp_path):
    (tmp_path / "secret").mkdir()
    results = tmp_path / "results"
    run = make_run_dir(results, [("P-1#1", "LLM07", Verdict.PASS, "ok")])
    c = _client(results)
    for baseline, current in (
        (run.name, "nope"),
        ("nope", run.name),
        (run.name, "../secret"),
        ("../secret", run.name),
        (run.name, run.name),  # the engine refuses the same folder twice
    ):
        r = c.get(f"{API}/compare", params={"baseline": baseline, "run": current})
        assert r.status_code == 404, (baseline, current)


def test_api_compare_needs_both_query_parameters(tmp_path):
    c = _client(tmp_path)
    assert c.get(f"{API}/compare").status_code == 422
    assert c.get(f"{API}/compare", params={"baseline": "a"}).status_code == 422


# --- the template helpers the views will use ----------------------------------------------------


def test_verdict_text_wraps_the_report_label():
    assert service.verdict_text(Verdict.FAIL) == "Fail"
    assert service.verdict_text(Verdict.NEEDS_REVIEW) == "Needs review"
    assert service.verdict_text(Verdict.PASS) == "Pass"
    assert service.verdict_text(Verdict.PASS, (DecidedBy.RULE,)) == "Pass (rule)"
    both = [DecidedBy.RULE, DecidedBy.HUMAN]
    assert service.verdict_text(Verdict.PASS, both) == verdict_label(Verdict.PASS, both)


def test_verdict_text_of_none_is_a_dash():
    assert service.verdict_text(None) == "—"
    assert service.verdict_text(None, (DecidedBy.RULE,)) == "—"


def test_legend_is_the_report_legend():
    assert service.LEGEND == VERDICT_LEGEND
