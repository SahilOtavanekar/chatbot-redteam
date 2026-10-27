"""The HTML views (HTMX + Jinja2): the second face over ``suite.web.service``, next to the JSON API.

Each route makes one service call and renders one template; there is no logic of its own. A run id
comes straight from the URL to the service, whose ``resolve_run_dir`` is the only thing that turns
it into a path. A read that finds nothing is a 404. A verdict or finalize that did not work comes
back as ``200`` with the reason in the fragment, because HTMX swaps only a 2xx response: an error
the reviewer cannot see would be worse than one they can.

Everything a bot said reaches a template as plain text, and the templates are autoescaped.
"""

from typing import Annotated, NoReturn

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from suite.web import service
from suite.web.deps import SettingsDep, TemplatesDep

# The pages are for people, not the API schema (``/openapi.json`` describes ``/api/v1`` only).
router = APIRouter(include_in_schema=False)

CLEAR = "clear"  # the form value of the "Clear" button: remove the saved verdict


def _not_found(what: str) -> NoReturn:
    raise HTTPException(status_code=404, detail=f"{what} not found")


@router.get("/")
def runs_page(request: Request, settings: SettingsDep, templates: TemplatesDep) -> HTMLResponse:
    return templates.TemplateResponse(request, "runs.html", {"runs": service.list_runs(settings)})


@router.get("/runs/{run_id}")
def run_page(
    request: Request, run_id: str, settings: SettingsDep, templates: TemplatesDep
) -> HTMLResponse:
    detail = service.get_run(settings, run_id)
    if detail is None:
        _not_found("run")
    return templates.TemplateResponse(request, "run.html", {"detail": detail})


@router.get("/runs/{run_id}/coverage")
def coverage_page(
    request: Request, run_id: str, settings: SettingsDep, templates: TemplatesDep
) -> HTMLResponse:
    rows = service.coverage(settings, run_id)
    if rows is None:
        _not_found("run")
    context = {"run_id": run_id, "rows": rows, "legend": service.LEGEND}
    return templates.TemplateResponse(request, "coverage.html", context)


@router.get("/runs/{run_id}/review")
def review_page(
    request: Request, run_id: str, settings: SettingsDep, templates: TemplatesDep
) -> HTMLResponse:
    listing = service.review_listing(settings, run_id)
    if listing is None:
        _not_found("run")
    context = {"run_id": run_id, "listing": listing}
    return templates.TemplateResponse(request, "review.html", context)


@router.post("/runs/{run_id}/review/{attempt_id}")
def save_verdict(
    request: Request,
    run_id: str,
    attempt_id: str,
    verdict: Annotated[str, Form()],
    settings: SettingsDep,
    templates: TemplatesDep,
    note: Annotated[str, Form()] = "",
) -> HTMLResponse:
    # FastAPI treats an empty form value as missing, so the Clear button sends a word, not "".
    word = "" if verdict == CLEAR else verdict
    write = service.set_review_verdict(settings, run_id, attempt_id, word, note)
    context = {
        "run_id": run_id,
        "attempt_id": attempt_id,
        "item": write.item,
        "error": write.error,
        "saved": write.ok,
        "note": note,  # what the reviewer typed, so a failed save can be retried in place
    }
    return templates.TemplateResponse(request, "_review_item.html", context)


@router.post("/runs/{run_id}/finalize")
def finalize(
    request: Request, run_id: str, settings: SettingsDep, templates: TemplatesDep
) -> HTMLResponse:
    outcome = service.finalize_run(settings, run_id)
    context = {"run_id": run_id, "outcome": outcome}
    return templates.TemplateResponse(request, "_finalize_status.html", context)


@router.get("/compare")
def compare_page(
    request: Request, baseline: str, run: str, settings: SettingsDep, templates: TemplatesDep
) -> HTMLResponse:
    view = service.compare(settings, baseline, run)
    if view is None:
        _not_found("comparison")
    return templates.TemplateResponse(request, "compare.html", {"view": view})
