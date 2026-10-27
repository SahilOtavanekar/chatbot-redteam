"""FastAPI dependencies shared by the JSON API and the HTML views.

Both read what ``create_app`` stored on ``app.state``. They live here, not in ``app.py``, so the
routers and the factory can all import them without importing each other.
"""

from typing import Annotated

from fastapi import Depends, Request
from starlette.templating import Jinja2Templates

from suite.web.settings import WebSettings


def get_settings(request: Request) -> WebSettings:
    """The ``WebSettings`` ``create_app`` stored on the app."""
    settings: WebSettings = request.app.state.settings
    return settings


def get_templates(request: Request) -> Jinja2Templates:
    """The Jinja2 templates ``create_app`` stored on the app (autoescape on, ``verdict`` filter)."""
    templates: Jinja2Templates = request.app.state.templates
    return templates


SettingsDep = Annotated[WebSettings, Depends(get_settings)]
TemplatesDep = Annotated[Jinja2Templates, Depends(get_templates)]
