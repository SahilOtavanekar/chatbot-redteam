"""The web app factory. It wires the settings, templates, static files and routers together.

Nothing here touches the engine: every route calls ``suite.web.service``, the one module that
does.
"""

from pathlib import Path

from fastapi import FastAPI
from jinja2 import Environment, PackageLoader, StrictUndefined
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from suite.web import api, service, views
from suite.web.settings import WebSettings

STATIC_DIR = Path(__file__).parent / "static"


def build_templates() -> Jinja2Templates:
    """The HTML templates. Autoescape is on for everything: a bot reply is untrusted data and
    must render inert. Every ``Verdict`` is printed through the ``verdict`` filter."""
    env = Environment(
        loader=PackageLoader("suite.web", "templates"),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["verdict"] = service.verdict_text
    return Jinja2Templates(env=env)


def create_app(settings: WebSettings) -> FastAPI:
    """Build the app: ``settings`` and ``templates`` on ``app.state``, ``/static`` mounted, the
    JSON API (``/api/v1``) and the HTML views included."""
    app = FastAPI(title="chatbot-redteam")
    app.state.settings = settings
    app.state.templates = build_templates()
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(api.router)
    app.include_router(views.router)
    return app
