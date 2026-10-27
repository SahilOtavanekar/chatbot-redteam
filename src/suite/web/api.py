"""The JSON API (``/api/v1``): one thin route per ``suite.web.service`` call.

A run id comes straight from the URL and goes straight to the service, whose ``resolve_run_dir``
is the only thing that turns an id into a path. A route turns the service's "no such thing"
(``None``) into a 404. A verdict or finalize that did not work is data, not a server fault: it
comes back as ``200`` with ``ok: false`` and the reason in ``error``.

The routes are plain ``def``: the service does blocking file and SQLite work, which FastAPI runs
in its thread pool (the service serialises the ``review.csv`` access itself).
"""

from dataclasses import dataclass
from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from suite.web import service
from suite.web.app import get_settings
from suite.web.settings import WebSettings

router = APIRouter(prefix="/api/v1")

SettingsDep = Annotated[WebSettings, Depends(get_settings)]


class VerdictBody(BaseModel):
    """The JSON body of a verdict write: ``pass``, ``fail`` or blank (clear it), and a note."""

    verdict: str
    note: str = ""


@dataclass(frozen=True)
class ReviewListing:
    """The review queue, or ``error`` (and no items) when ``review.csv`` cannot be read."""

    items: list[service.ReviewItem]
    error: str | None = None


def _not_found(what: str) -> NoReturn:
    raise HTTPException(status_code=404, detail=f"{what} not found")


@router.get("/runs")
def list_runs(settings: SettingsDep) -> list[service.RunSummary]:
    return service.list_runs(settings)


@router.get("/runs/{run_id}")
def get_run(run_id: str, settings: SettingsDep) -> service.RunDetail:
    detail = service.get_run(settings, run_id)
    if detail is None:
        _not_found("run")
    return detail


@router.get("/runs/{run_id}/coverage")
def get_coverage(run_id: str, settings: SettingsDep) -> list[service.CoverageRow]:
    rows = service.coverage(settings, run_id)
    if rows is None:
        _not_found("run")
    return rows


@router.get("/runs/{run_id}/review")
def get_review(run_id: str, settings: SettingsDep) -> ReviewListing:
    try:
        items = service.review_queue(settings, run_id)
    except service.ConfigError as exc:  # a corrupt or locked review.csv: say so, don't 500
        return ReviewListing(items=[], error=str(exc))
    if items is None:
        _not_found("run")
    return ReviewListing(items=items)


@router.put("/runs/{run_id}/review/{attempt_id}")
def put_review_verdict(
    run_id: str, attempt_id: str, body: VerdictBody, settings: SettingsDep
) -> service.VerdictWrite:
    return service.set_review_verdict(settings, run_id, attempt_id, body.verdict, body.note)


@router.post("/runs/{run_id}/finalize")
def post_finalize(run_id: str, settings: SettingsDep) -> service.FinalizeOutcome:
    return service.finalize_run(settings, run_id)


@router.get("/compare")
def get_compare(baseline: str, run: str, settings: SettingsDep) -> service.CompareView:
    view = service.compare(settings, baseline, run)
    if view is None:
        _not_found("comparison")
    return view
