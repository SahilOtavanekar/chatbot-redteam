"""Tests for the web app's request guard: same-origin writes, trusted hosts, no framing.

The loopback bind does not stop a browser from sending a request to ``127.0.0.1``: any page the
operator has open can post a form there, or reach it through a domain rebound to 127.0.0.1. So the
verdict (Pass/Fail) must not be writable cross-site, nor an irreversible finalize triggerable.
"""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from suite.models import Verdict
from suite.report import write_reports
from suite.web import service
from suite.web.app import create_app
from suite.web.settings import WebSettings
from tests.factories import make_run_dir

API = "/api/v1"
EVIL = {"Origin": "http://evil.example"}
REFUSED = {"detail": "cross-origin request refused"}


def _settings(results_dir: Path) -> WebSettings:
    return WebSettings(results_dir=results_dir)


def _client(results_dir: Path, base_url: str = "http://testserver") -> TestClient:
    return TestClient(create_app(_settings(results_dir)), base_url=base_url)


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


def _decided_run(root: Path) -> Path:
    """A queued run whose one review verdict is already saved, so a finalize would succeed."""
    run = _queued_run(root)
    assert service.set_review_verdict(_settings(root), run.name, "P-2#1", "fail", "").ok
    return run


def _verdict_url(run: Path) -> str:
    return f"/runs/{run.name}/review/P-2%231"


def _finalized(root: Path, run: Path) -> bool:
    (summary,) = [s for s in service.list_runs(_settings(root)) if s.run_id == run.name]
    return summary.finalized


# --- a cross-origin write is refused, and never reaches the handler ----------------------------


def test_cross_origin_html_finalize_is_refused_and_does_not_finalize(tmp_path):
    run = _decided_run(tmp_path)
    r = _client(tmp_path).post(f"/runs/{run.name}/finalize", headers=EVIL)
    assert r.status_code == 403 and r.json() == REFUSED
    assert not _finalized(tmp_path, run)


def test_cross_origin_html_verdict_is_refused_and_review_csv_is_untouched(tmp_path):
    run = _queued_run(tmp_path)
    before = (run / "review.csv").read_bytes()
    r = _client(tmp_path).post(
        _verdict_url(run), data={"verdict": "pass", "note": "forged"}, headers=EVIL
    )
    assert r.status_code == 403 and r.json() == REFUSED
    assert (run / "review.csv").read_bytes() == before


def test_cross_origin_api_finalize_is_refused_and_does_not_finalize(tmp_path):
    run = _decided_run(tmp_path)
    r = _client(tmp_path).post(f"{API}/runs/{run.name}/finalize", headers=EVIL)
    assert r.status_code == 403 and r.json() == REFUSED
    assert not _finalized(tmp_path, run)


def test_cross_origin_api_verdict_is_refused_and_review_csv_is_untouched(tmp_path):
    run = _queued_run(tmp_path)
    before = (run / "review.csv").read_bytes()
    r = _client(tmp_path).put(
        f"{API}/runs/{run.name}/review/P-2%231",
        json={"verdict": "pass", "note": "forged"},
        headers=EVIL,
    )
    assert r.status_code == 403 and r.json() == REFUSED
    assert (run / "review.csv").read_bytes() == before


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_every_unsafe_method_is_refused_cross_origin_even_on_an_unrouted_path(tmp_path, method):
    # The guard sits in front of the router, so it does not depend on a route existing.
    r = getattr(_client(tmp_path), method)("/anything", headers=EVIL)
    assert r.status_code == 403 and r.json() == REFUSED


@pytest.mark.parametrize(
    "origin",
    [
        "http://testserver:8787",  # same host, another port: another origin
        "http://testserver.evil.example",  # the host as a prefix of another
        "http://evil.example/http://testserver",
        "null",  # sandboxed frames and some redirects send the literal word
        "testserver",  # no scheme
        "http://",  # scheme only
        "",  # present but empty
        "not an origin",
    ],
)
def test_a_mismatched_or_malformed_origin_is_refused(tmp_path, origin):
    run = _decided_run(tmp_path)
    r = _client(tmp_path).post(f"/runs/{run.name}/finalize", headers={"Origin": origin})
    assert r.status_code == 403 and r.json() == REFUSED
    assert not _finalized(tmp_path, run)


# --- a same-origin or origin-less write still works --------------------------------------------


def test_same_origin_html_finalize_is_allowed(tmp_path):
    run = _decided_run(tmp_path)
    r = _client(tmp_path).post(
        f"/runs/{run.name}/finalize", headers={"Origin": "http://testserver"}
    )
    assert r.status_code == 200 and "Finalized" in r.text
    assert _finalized(tmp_path, run)


def test_same_origin_html_verdict_is_allowed(tmp_path):
    run = _queued_run(tmp_path)
    r = _client(tmp_path).post(
        _verdict_url(run),
        data={"verdict": "pass", "note": "fine"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200 and "Saved: pass" in r.text


def test_same_origin_api_finalize_and_verdict_are_allowed(tmp_path):
    run = _queued_run(tmp_path)
    same = {"Origin": "http://testserver"}
    c = _client(tmp_path)
    r = c.put(
        f"{API}/runs/{run.name}/review/P-2%231", json={"verdict": "pass", "note": ""}, headers=same
    )
    assert r.status_code == 200
    r = c.post(f"{API}/runs/{run.name}/finalize", headers=same)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert _finalized(tmp_path, run)


def test_same_origin_on_the_real_loopback_host_and_port_is_allowed(tmp_path):
    run = _decided_run(tmp_path)
    c = _client(tmp_path, "http://127.0.0.1:8787")
    r = c.post(f"/runs/{run.name}/finalize", headers={"Origin": "http://127.0.0.1:8787"})
    assert r.status_code == 200 and "Finalized" in r.text


def test_same_origin_matches_regardless_of_case(tmp_path):
    run = _decided_run(tmp_path)
    c = _client(tmp_path, "http://localhost:8787")
    r = c.post(f"/runs/{run.name}/finalize", headers={"Origin": "http://LocalHost:8787"})
    assert r.status_code == 200


def test_a_post_without_an_origin_is_allowed(tmp_path):
    # curl, a same-origin navigation and the existing tests send no Origin at all.
    run = _decided_run(tmp_path)
    r = _client(tmp_path).post(f"/runs/{run.name}/finalize")
    assert r.status_code == 200 and "Finalized" in r.text
    assert _finalized(tmp_path, run)


def test_an_api_post_without_an_origin_is_allowed(tmp_path):
    run = _decided_run(tmp_path)
    r = _client(tmp_path).post(f"{API}/runs/{run.name}/finalize")
    assert r.status_code == 200 and r.json()["ok"] is True


# --- a read is never blocked by the origin rule ------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/", "/runs/{run}", "/runs/{run}/review", f"{API}/runs", f"{API}/runs/{{run}}"],
)
def test_a_get_with_a_cross_origin_header_still_returns_the_page(tmp_path, path):
    run = _queued_run(tmp_path)
    r = _client(tmp_path).get(path.format(run=run.name), headers=EVIL)
    assert r.status_code == 200


@pytest.mark.parametrize("method", ["head", "options"])
def test_head_and_options_are_not_refused_by_the_origin_rule(tmp_path, method):
    r = getattr(_client(tmp_path), method)("/", headers=EVIL)
    assert r.status_code != 403


# --- the Host header must be a loopback name (DNS rebinding) -----------------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:8787",
        "http://127.0.0.1",
        "http://localhost",
        "http://localhost:8787",
        "http://[::1]:8787",
        "http://testserver",
    ],
)
def test_loopback_and_test_hosts_are_trusted(tmp_path, base_url):
    assert _client(tmp_path, base_url).get("/").status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:8787",
        "127.0.0.1.evil.example",
        "localhost.evil.example",
        "evil.example:127.0.0.1",
        "0.0.0.0:8787",
        "192.168.1.5:8787",
    ],
)
def test_an_untrusted_host_is_rejected_before_any_route(tmp_path, host):
    c = _client(tmp_path)
    for path in ("/", f"{API}/runs"):
        r = c.get(path, headers={"Host": host})
        assert r.status_code == 400


def test_a_rebound_domain_cannot_write_even_with_a_matching_origin(tmp_path):
    # DNS rebinding: the page's origin IS the rebound domain, so Origin equals Host and the
    # same-origin rule alone passes. The host check is what stops it.
    run = _decided_run(tmp_path)
    r = _client(tmp_path).post(
        f"/runs/{run.name}/finalize",
        headers={"Host": "evil.example:8787", "Origin": "http://evil.example:8787"},
    )
    assert r.status_code == 400
    assert not _finalized(tmp_path, run)


# --- the pages cannot be framed ----------------------------------------------------------------


def test_every_response_forbids_framing(tmp_path):
    run = _decided_run(tmp_path)
    c = _client(tmp_path)
    responses = [
        c.get("/"),
        c.get(f"/runs/{run.name}"),
        c.get(f"{API}/runs"),
        c.get("/static/app.css"),
        c.get("/no-such-page"),  # a 404 page is a response too
        c.post(f"/runs/{run.name}/finalize", headers=EVIL),  # the 403 refusal
        c.get("/", headers={"Host": "evil.example"}),  # the 400 for a bad host
    ]
    assert [r.status_code for r in responses] == [200, 200, 200, 200, 404, 403, 400]
    for r in responses:
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["content-security-policy"] == "frame-ancestors 'none'"
