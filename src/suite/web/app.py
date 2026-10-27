"""The web app factory. It wires the settings, the static files and the routers together.

Nothing here touches the engine: every route calls ``suite.web.service``, the one module that
does.
"""

from pathlib import Path

from fastapi import FastAPI, Request
from starlette.staticfiles import StaticFiles

from suite.web.settings import WebSettings

STATIC_DIR = Path(__file__).parent / "static"


def get_settings(request: Request) -> WebSettings:
    """FastAPI dependency: the ``WebSettings`` ``create_app`` stored on the app."""
    settings: WebSettings = request.app.state.settings
    return settings


def create_app(settings: WebSettings) -> FastAPI:
    """Build the app: ``settings`` on ``app.state``, ``/static`` mounted, the JSON API included.

    The routers import ``get_settings`` from this module, so they are imported here, when the app
    is built, rather than at the top of the file.
    """
    from suite.web import api

    app = FastAPI(title="chatbot-redteam")
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(api.router)
    return app
