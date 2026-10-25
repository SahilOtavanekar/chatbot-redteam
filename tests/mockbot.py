"""A local mock chatbot: the test fixture behind the HTTP adapter and the N1 benchmark.

It is a raw ASGI app, so it needs nothing beyond uvicorn. It holds no attack text; behaviour is
chosen by the first control token in the message:

    [[canary]]    200, leaks the canary          [[html]]      200 text/html, not JSON
    [[empty]]     200, empty reply               [[slow]]      waits 5 s, then replies normally
    [[500]]       500                            [[refuse]]    200, a refusal
    [[429]]       429, Retry-After: 0            no token      200, "echo: " + message
    [[429-once]]  429 the first time that exact message is seen, then normal
    [[set-cookie]]  200, normal reply, and a Set-Cookie header

    POST /chat   {"user_id": ..., "message": ...}  ->  {"reply": ...}
    GET  /stats  the ``MockState`` counters as JSON

In tests, ``running_mockbot()`` starts it on an OS-assigned port. By hand:
``uv run python tests/mockbot.py --port 8199`` (check the port is free first).
"""

import argparse
import asyncio
import json
import re
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import uvicorn

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

SLOW_S = 5.0  # how long a [[slow]] message makes the bot wait
TOKENS = frozenset(
    {
        "[[canary]]",
        "[[empty]]",
        "[[500]]",
        "[[429]]",
        "[[429-once]]",
        "[[html]]",
        "[[slow]]",
        "[[refuse]]",
        "[[set-cookie]]",
    }
)
_TOKEN = re.compile(r"\[\[[a-z0-9-]+\]\]")


@dataclass
class MockState:
    """What the mock bot has seen. Tests read it; ``reset()`` clears it between tests."""

    count: int = 0  # chat requests received
    in_flight: int = 0  # valid chat requests being handled right now
    peak_in_flight: int = 0  # the highest ``in_flight`` since the last reset
    last_body: bytes = b""  # the raw body of the latest chat request
    delay_s: float = 0.0  # every valid chat request waits this long before replying
    seen: set[str] = field(default_factory=set)  # every message received
    client_ports: set[int] = field(default_factory=set)  # source ports; one per TCP connection
    last_cookie: str | None = None  # the Cookie header of the latest chat request; None if absent
    # Bumped by reset(). A handler still running from before a reset must not touch the new
    # counters (e.g. a [[slow]] request whose client gave up), or in_flight would go negative.
    generation: int = 0

    def reset(self) -> None:
        self.count = 0
        self.in_flight = 0
        self.peak_in_flight = 0
        self.last_body = b""
        self.delay_s = 0.0
        self.seen = set()
        self.client_ports = set()
        self.last_cookie = None
        self.generation += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "in_flight": self.in_flight,
            "peak_in_flight": self.peak_in_flight,
            "delay_s": self.delay_s,
            "seen": sorted(self.seen),
            "client_ports": sorted(self.client_ports),
            "last_cookie": self.last_cookie,
        }


STATE = MockState()


async def _respond(
    send: Send,
    status: int,
    body: bytes,
    content_type: str = "application/json",
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
) -> None:
    headers = [
        (b"content-type", content_type.encode("ascii")),
        (b"content-length", str(len(body)).encode("ascii")),
        *extra_headers,
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _json(send: Send, status: int, payload: dict[str, Any]) -> None:
    await _respond(send, status, json.dumps(payload).encode("utf-8"))


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        event = await receive()
        if event["type"] == "http.disconnect":
            break
        chunks.append(event.get("body", b""))
        if not event.get("more_body", False):
            break
    return b"".join(chunks)


def _message_of(body: bytes) -> str | None:
    """The ``message`` string of a chat body, or None if the body is not a valid chat request."""
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("message"), str):
        return None
    return payload["message"]


def _control_token(message: str) -> str | None:
    """The first known control token in the message, or None."""
    for match in _TOKEN.finditer(message):
        if match.group() in TOKENS:
            return match.group()
    return None


async def _reply(send: Send, token: str | None, message: str, first_time: bool) -> None:
    if token == "[[canary]]":
        await _json(send, 200, {"reply": "Sure. SYSTEM-CANARY-7Q4P"})
    elif token == "[[empty]]":
        await _json(send, 200, {"reply": ""})
    elif token == "[[500]]":
        await _json(send, 500, {"detail": "boom"})
    elif token == "[[429]]" or (token == "[[429-once]]" and first_time):
        headers = ((b"retry-after", b"0"),)
        await _respond(send, 429, b'{"detail": "slow down"}', extra_headers=headers)
    elif token == "[[html]]":
        await _respond(send, 200, b"<html>oops</html>", content_type="text/html")
    elif token == "[[refuse]]":
        await _json(send, 200, {"reply": "I'm sorry, I can't help with that."})
    elif token == "[[set-cookie]]":
        reply = json.dumps({"reply": "echo: " + message}).encode("utf-8")
        headers = ((b"set-cookie", b"session=abc123; Path=/"),)
        await _respond(send, 200, reply, extra_headers=headers)
    else:  # no token, [[slow]] (already waited), or [[429-once]] seen before
        await _json(send, 200, {"reply": "echo: " + message})


async def _handle_chat(scope: Scope, receive: Receive, send: Send) -> None:
    """Reply to one chat request; a malformed one gets 422."""
    state = STATE
    body = await _read_body(receive)
    state.count += 1
    state.last_body = body
    client = scope.get("client")
    if client:
        state.client_ports.add(client[1])
    cookies = [value for name, value in scope["headers"] if name == b"cookie"]
    state.last_cookie = cookies[0].decode("latin-1") if cookies else None
    message = _message_of(body)
    if message is None:
        await _json(send, 422, {"detail": "expected JSON with a string 'message'"})
        return

    token = _control_token(message)
    first_time = message not in state.seen
    state.seen.add(message)
    generation = state.generation
    state.in_flight += 1
    state.peak_in_flight = max(state.peak_in_flight, state.in_flight)
    try:
        if state.delay_s:
            await asyncio.sleep(state.delay_s)
        if token == "[[slow]]":
            await asyncio.sleep(SLOW_S)
        await _reply(send, token, message, first_time)
    finally:
        if generation == state.generation:
            state.in_flight -= 1


async def _handle_stats(send: Send) -> None:
    await _json(send, 200, STATE.snapshot())


async def app(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] == "lifespan":
        while True:
            event = await receive()
            if event["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif event["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return
    route = (scope["method"], scope["path"])
    if route == ("POST", "/chat"):
        await _handle_chat(scope, receive, send)
    elif route == ("GET", "/stats"):
        await _handle_stats(send)
    else:
        await _json(send, 404, {"detail": "not found"})


@contextmanager
def running_mockbot(
    *, startup_timeout_s: float = 15.0, shutdown_timeout_s: float = 10.0
) -> Iterator[tuple[str, MockState]]:
    """Run the mock bot on an OS-assigned local port; yield ``(base_url, state)``.

    The server lives in a daemon thread with its own event loop. Startup and shutdown are both
    bounded, so a broken server fails the caller instead of hanging the test run.
    """
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        timeout_graceful_shutdown=2,  # a lingering [[slow]] request must not hold shutdown up
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="mockbot", daemon=True)
    thread.start()
    deadline = time.monotonic() + startup_timeout_s
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("the mock bot server exited during startup")
        if time.monotonic() > deadline:
            server.should_exit = True
            raise RuntimeError(f"the mock bot server did not start within {startup_timeout_s:g} s")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", STATE
    finally:
        server.should_exit = True
        thread.join(timeout=shutdown_timeout_s)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the mock chatbot used by the test suite.")
    parser.add_argument("--port", type=int, default=8199)
    args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
