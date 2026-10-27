"""Tests for the PyRIT-backed HTTP sender, run against the local mock bot (tests/mockbot.py).

Every test is Executed: it builds the real sender (real PyRIT HTTPTarget, real httpx) and talks
to the mock bot over a real socket on an OS-assigned port. No external service is contacted.
"""

import json
import socket

import pytest

from suite.adapters import build_sender
from suite.adapters.http import PROMPT_MARKER, build_raw_request
from suite.models import SendError
from tests.factories import make_spec
from tests.mockbot import MockState

# --- build_raw_request ------------------------------------------------------------------------


def test_build_raw_request():
    spec = make_spec(
        url="http://127.0.0.1:9/chat?x=1",
        headers={"X-Key": "k"},
        body={"user_id": "alice", "message": "Q: {prompt}"},
    )
    assert build_raw_request(spec) == (
        "POST /chat?x=1 HTTP/1.1\nHost: 127.0.0.1:9\n"
        'Content-Type: application/json\nX-Key: k\n\n{"user_id": "alice", "message": "Q: {PROMPT}"}'
    )


def test_marker_is_pyrits_default_placeholder():
    assert PROMPT_MARKER == "{PROMPT}"


def test_raw_request_keeps_a_configured_content_type_in_any_case():
    spec = make_spec(headers={"content-type": "application/vnd.x+json", "X-Key": "k"})
    lines = build_raw_request(spec).split("\n\n")[0].split("\n")
    assert lines == [
        "POST /chat HTTP/1.1",
        "Host: 127.0.0.1:9",
        "content-type: application/vnd.x+json",
        "X-Key: k",
    ]


def test_raw_request_with_a_bare_url_uses_the_root_path_and_the_put_method():
    request = build_raw_request(make_spec(url="https://bot.example.com", method="PUT"))
    assert request.startswith("PUT / HTTP/1.1\nHost: bot.example.com\n")


def test_raw_request_leaves_non_ascii_body_text_unescaped():
    spec = make_spec(body={"message": "naïve {prompt}"})
    assert build_raw_request(spec).endswith('{"message": "naïve {PROMPT}"}')


def test_raw_request_host_header_never_carries_url_credentials():
    # Config refuses a url with user:password@; the adapter still strips it as a backstop, so
    # the spec here skips validation (model_copy does not validate).
    spec = make_spec().model_copy(update={"url": "http://user:secret@127.0.0.1:9/chat"})
    request = build_raw_request(spec)
    assert "secret" not in request
    assert "Host: 127.0.0.1:9\n" in request


@pytest.mark.parametrize(
    ("url", "use_tls", "built_url"),
    [
        ("https://bot.example.com/chat", True, "https://bot.example.com/chat"),
        ("HTTPS://bot.example.com/chat", True, "https://bot.example.com/chat"),
        ("http://bot.example.com/chat", False, "http://bot.example.com/chat"),
        ("HTTP://bot.example.com/chat", False, "http://bot.example.com/chat"),
    ],
)
async def test_tls_follows_the_parsed_scheme_in_any_case(pyrit_ready, url, use_tls, built_url):
    # An upper-case HTTPS scheme must never downgrade to plaintext (header secrets travel too).
    spec = make_spec(url=url, headers={"X-Key": "k"})
    sender = build_sender(spec)
    try:
        assert sender._target.use_tls is use_tls
        assert sender._target.parse_raw_http_request(build_raw_request(spec))[2] == built_url
    finally:
        await sender.aclose()


# --- sending against the mock bot -------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        'say "hi"',
        "back\\slash",
        "two\nlines",
        "tab\tend",
        "naïve – 日本語 – 😀",
        "literal {PROMPT} marker",  # PyRIT's own placeholder must not be substituted twice
        "literal {prompt} placeholder",
    ],
)
async def test_send_roundtrips_tricky_text(mockbot, pyrit_ready, text):  # Review Focus 1
    url, state = mockbot
    sender = build_sender(make_spec(url=f"{url}/chat"))
    try:
        assert await sender.send(text) == f"echo: {text}"
        assert json.loads(state.last_body)["message"] == text
    finally:
        await sender.aclose()


async def test_body_template_reaches_the_bot_intact(mockbot, pyrit_ready):
    url, state = mockbot
    spec = make_spec(url=f"{url}/chat", body={"user_id": "alice", "message": "Q: {prompt}"})
    sender = build_sender(spec)
    try:
        assert await sender.send("hi") == "echo: Q: hi"
        assert json.loads(state.last_body) == {"user_id": "alice", "message": "Q: hi"}
    finally:
        await sender.aclose()


async def test_a_control_token_in_the_text_reaches_the_bot(mockbot, pyrit_ready):
    url, _ = mockbot
    sender = build_sender(make_spec(url=f"{url}/chat"))
    try:
        assert await sender.send("please [[canary]]") == "Sure. SYSTEM-CANARY-7Q4P"
    finally:
        await sender.aclose()


async def test_non_2xx_raises_http_status(mockbot, pyrit_ready):
    url, _ = mockbot
    sender = build_sender(make_spec(url=f"{url}/chat"))
    try:
        with pytest.raises(SendError) as caught:
            await sender.send("[[500]]")
        assert caught.value.kind == "http_status"
        assert caught.value.status == 500
        with pytest.raises(SendError) as throttled:
            await sender.send("[[429]]")
        assert throttled.value.kind == "http_status"
        assert throttled.value.status == 429
        assert throttled.value.retry_after == 0
    finally:
        await sender.aclose()


async def test_empty_and_html_replies(mockbot, pyrit_ready):
    url, _ = mockbot
    sender = build_sender(make_spec(url=f"{url}/chat"))
    try:
        with pytest.raises(SendError) as empty:
            await sender.send("[[empty]]")
        assert empty.value.kind == "empty_reply"
        with pytest.raises(SendError) as html:
            await sender.send("[[html]]")
        assert html.value.kind == "unreadable_reply"
    finally:
        await sender.aclose()


async def test_reply_path_selects_the_reply_field(mockbot, pyrit_ready):
    url, _ = mockbot
    sender = build_sender(make_spec(url=f"{url}/chat", reply="$.missing"))
    try:
        with pytest.raises(SendError) as caught:
            await sender.send("hi")
        assert caught.value.kind == "unreadable_reply"
    finally:
        await sender.aclose()


async def test_timeout_maps_to_timeout(mockbot, pyrit_ready):
    url, _ = mockbot
    sender = build_sender(make_spec(url=f"{url}/chat", limits=dict(timeout_s=0.5)))
    try:
        with pytest.raises(SendError) as caught:
            await sender.send("[[slow]]")
        assert caught.value.kind == "timeout"
    finally:
        await sender.aclose()


async def test_connection_refused_is_transport(pyrit_ready):
    with socket.socket() as probe:  # a port that was free a moment ago and is closed again
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    sender = build_sender(make_spec(url=f"http://127.0.0.1:{port}/chat"))
    try:
        with pytest.raises(SendError) as caught:
            await sender.send("hi")
        assert caught.value.kind == "transport"
    finally:
        await sender.aclose()


async def test_reuses_connections(mockbot, pyrit_ready):
    url, state = mockbot
    sender = build_sender(make_spec(url=f"{url}/chat"))
    try:
        for _ in range(20):
            await sender.send("hi")
        assert state.count == 20
        assert len(state.client_ports) <= 2
    finally:
        await sender.aclose()


async def test_server_set_cookies_are_not_sent_back(mockbot, pyrit_ready):
    # Every attempt is single-turn: a bot's session cookie must not tie replies together.
    url, state = mockbot
    sender = build_sender(make_spec(url=f"{url}/chat"))
    try:
        assert await sender.send("first [[set-cookie]]") == "echo: first [[set-cookie]]"
        assert state.last_cookie is None
        await sender.send("second")
        assert state.last_cookie is None
        await sender.send("third")
        assert state.last_cookie is None
    finally:
        await sender.aclose()


async def test_a_cookie_header_from_the_spec_is_sent_on_every_request(mockbot, pyrit_ready):
    url, state = mockbot
    sender = build_sender(make_spec(url=f"{url}/chat", headers={"Cookie": "a=b"}))
    try:
        await sender.send("first [[set-cookie]]")
        assert state.last_cookie == "a=b"
        await sender.send("second")
        assert state.last_cookie == "a=b"  # the server's cookie is not added to it
        await sender.send("third")
        assert state.last_cookie == "a=b"
    finally:
        await sender.aclose()


async def test_aclose_closes_the_shared_client(mockbot, pyrit_ready):
    url, _ = mockbot
    sender = build_sender(make_spec(url=f"{url}/chat"))
    await sender.send("hi")
    await sender.aclose()
    with pytest.raises(RuntimeError, match="closed"):  # httpx refuses a closed client
        await sender.send("hi")


def test_build_sender_rejects_a_type_it_has_no_adapter_for():
    spec = make_spec()
    spec.type = "playwright"  # config does not accept this type until v0.2; assignment skips it
    with pytest.raises(ValueError, match="playwright"):
        build_sender(spec)


def test_mock_state_reset_clears_everything():
    state = MockState()
    state.count = 3
    state.peak_in_flight = 2
    state.last_body = b"x"
    state.delay_s = 1.0
    state.seen.add("m")
    state.client_ports.add(1)
    state.last_cookie = "a=b"
    state.reset()
    assert state.last_cookie is None
    assert (state.count, state.in_flight, state.peak_in_flight) == (0, 0, 0)
    assert (state.last_body, state.delay_s, state.seen, state.client_ports) == (
        b"",
        0.0,
        set(),
        set(),
    )
