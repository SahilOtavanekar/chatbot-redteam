"""The web app factory. It wires the settings, templates, static files and routers together.

Nothing here touches the engine: every route calls ``suite.web.service``, the one module that
does.

It also guards every route in one place, for both faces (the JSON API and the HTML views). Binding
to loopback does not stop a browser from sending a request to ``127.0.0.1``, so a page the operator
has open could post a form there, or reach it through a domain rebound to 127.0.0.1. Three
middleware close that: a trusted-host check (rebinding), a same-origin check on every write
(cross-site forgery), and a no-framing header (clickjacking).
"""

from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from jinja2 import Environment, PackageLoader, StrictUndefined
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from suite.web import api, service, views
from suite.web.settings import WebSettings

STATIC_DIR = Path(__file__).parent / "static"

# The ``Host`` names the app always answers to. ``testserver`` is the test client's own host.
# ``[::1]`` is how a browser writes the IPv6 loopback in a Host header (the bare ``::1`` is kept
# too, in case a client sends it that way). A non-loopback ``--host`` adds its own name to these
# (see ``allowed_hosts``).
TRUSTED_HOSTS = ["127.0.0.1", "localhost", "::1", "[::1]", "testserver"]

# Bind addresses that mean "every interface". Nothing can be pinned for them: whatever address a
# client uses to reach the machine is the ``Host`` it sends.
WILDCARD_BINDS = frozenset({"0.0.0.0", "::", ""})

# Methods that change nothing, so a cross-origin one is harmless (the browser's same-origin policy
# already stops the other page from reading the reply). Every other method is a write.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

CROSS_ORIGIN_REFUSED = {"detail": "cross-origin request refused"}


def _origin_host(origin: str) -> str | None:
    """The ``host[:port]`` of an ``Origin`` header (``scheme://host[:port]``), lower-cased, or
    ``None`` if it is not that form: ``null``, no scheme, a path, a query, or anything else."""
    try:
        parts = urlsplit(origin)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    if parts.path or parts.query or parts.fragment:
        return None
    return parts.netloc.lower()


class SameOriginGuard:
    """Refuse a write that a page on another origin sent.

    A browser puts ``Origin`` on every cross-origin write, and the page cannot change or drop it.
    So a write whose ``Origin`` is present and is not this server's own ``Host`` is refused with
    ``403`` before any route runs. A write with no ``Origin`` (curl, a script, the test client)
    is a tool, not another site's page, and passes; so does every read. This sits in front of the
    router, so it covers the HTML form posts and the JSON ``PUT``/``POST`` alike.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            headers = Headers(scope=scope)
            host = headers.get("host", "").strip().lower()
            if any(_origin_host(origin) != host for origin in headers.getlist("origin")):
                await JSONResponse(CROSS_ORIGIN_REFUSED, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class NoFraming:
    """Put ``X-Frame-Options: DENY`` (and the modern ``frame-ancestors 'none'``) on every response,
    so a hostile page cannot frame the app and trick the operator into clicking Pass."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_without_framing(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Frame-Options"] = "DENY"
                headers["Content-Security-Policy"] = "frame-ancestors 'none'"
            await send(message)

        await self.app(scope, receive, send_without_framing)


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


def allowed_hosts(bind_host: str) -> list[str]:
    """The ``Host`` values the app answers to when it listens on ``bind_host``.

    The loopback names (and ``testserver``) are always trusted. A concrete ``bind_host`` (a LAN
    address or a name) is added, since that is what a client reaching the app sends; a bare IPv6
    literal also gets its bracketed form, the only way a Host header can carry one. A wildcard bind
    (``0.0.0.0``, ``::`` or empty) answers to any ``Host``: the operator has opted out of the
    local-only model, and ``suite web`` warns about it."""
    if bind_host in WILDCARD_BINDS:
        return ["*"]
    hosts = [*TRUSTED_HOSTS, bind_host]
    if ":" in bind_host and not bind_host.startswith("["):
        hosts.append(f"[{bind_host}]")
    return hosts


def create_app(settings: WebSettings) -> FastAPI:
    """Build the app: ``settings`` and ``templates`` on ``app.state``, ``/static`` mounted, the
    JSON API (``/api/v1``) and the HTML views included, behind the request guard.

    The middleware are listed outermost first, which is the order a request meets them: the
    no-framing header is outermost so that even a refusal carries it; the host check comes next, so
    a rebound domain never reaches the origin check (whose test it would pass, as its ``Origin``
    equals its ``Host``); the origin check is last, in front of the router."""
    app = FastAPI(
        title="chatbot-redteam",
        middleware=[
            Middleware(NoFraming),
            Middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts(settings.host)),
            Middleware(SameOriginGuard),
        ],
    )
    app.state.settings = settings
    app.state.templates = build_templates()
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(api.router)
    app.include_router(views.router)
    return app
