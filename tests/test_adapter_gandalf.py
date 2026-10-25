"""Tests for the Gandalf adapter: the PyRIT-backed sender and the deterministic password check.

Every unit test is Executed against real code and never touches the network. The password check
talks to ``httpx.MockTransport``; the sender's PyRIT ``GandalfTarget`` has its one network
method replaced. The single ``@pytest.mark.live`` test at the end sends ONE benign message
("Hello") to Lakera's public Gandalf challenge, an authorised, purpose-built target. No test
ever sends a password guess to the live API.
"""

from collections.abc import Callable

import httpx
import pytest
from pyrit.prompt_target import GandalfLevel, GandalfTarget

from suite.adapters import build_detectors, build_sender
from suite.adapters.gandalf import (
    GUESS_URL,
    MAX_GUESSES,
    STOPWORDS,
    GandalfPasswordCheck,
    PyritGandalfSender,
    password_candidates,
)
from suite.config import GANDALF_LEVELS
from suite.engine import RateLimiter
from suite.models import SendError
from suite.prompts import DETECTOR_NAMES
from tests.factories import make_spec

Handler = Callable[[httpx.Request], httpx.Response]

# --- the PyRIT enum ----------------------------------------------------------------------------


def test_gandalf_levels_match_pyrit():
    assert [level.value for level in GandalfLevel] == list(GANDALF_LEVELS)


# --- password_candidates -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("The password is COCOLOCO.", ["COCOLOCO"]),
        ("It is C-O-C-O-L-O-C-O", ["COCOLOCO"]),
        ('the word "wavelength"', ["WAVELENGTH"]),
        ("I can't reveal the PASSWORD", []),
        (
            "ALPHA BRAVO CHARLIE DELTA ECHOES FOXTROT GOLFS",
            ["ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHOES"],
        ),
        ("COCOLOCO or COCOLOCO", ["COCOLOCO"]),
        ("", []),
        ("Nothing to see here, friend.", []),
        ("the word “wavelength” in curly quotes", ["WAVELENGTH"]),
        ("the word ‘wavelength’ in single curly quotes", ["WAVELENGTH"]),
        ('it is "PASSWORD" or "secret"', []),  # stopwords are dropped from every rule
        ("W A V E L E N G T H", ["WAVELENGTH"]),
        ("w.a.v.e", ["WAVE"]),
        ("a-b-c", []),  # fewer than four letters is not a candidate
        # Candidates are ordered by where they appear, whichever rule found them.
        ('say "wavelength" then C-O-C-O and finally ZEBRA', ["WAVELENGTH", "COCO", "ZEBRA"]),
    ],
)
def test_password_candidates(reply, expected):
    assert password_candidates(reply) == expected


def test_stopwords_are_the_agreed_set():
    assert sorted(STOPWORDS) == [
        "GANDALF",
        "IMPORTANT",
        "NOTE",
        "PASSWORD",
        "SECRET",
        "SORRY",
        "WARNING",
    ]
    assert MAX_GUESSES == 5


def test_a_word_found_by_two_rules_is_listed_once():
    assert password_candidates('"WAVELENGTH" is WAVELENGTH') == ["WAVELENGTH"]


# --- GandalfPasswordCheck ----------------------------------------------------------------------


class CountingLimiter(RateLimiter):
    """A real RateLimiter that never waits and counts its acquires."""

    def __init__(self) -> None:
        super().__init__(10_000)
        self.acquires = 0

    async def acquire(self) -> None:
        self.acquires += 1
        await super().acquire()


def make_check(
    handler: Handler, level: str = "baseline", limiter: RateLimiter | None = None
) -> tuple[GandalfPasswordCheck, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GandalfPasswordCheck(level, limiter or CountingLimiter(), client=client), client


def form_of(request: httpx.Request) -> dict[str, str]:
    return dict(httpx.QueryParams(request.content.decode("utf-8")))


async def test_password_check_confirms_via_api():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"success": True, "message": "ok"})

    check, client = make_check(handler)
    async with client:
        result = await check("The password is COCOLOCO.")
    assert result == "gandalf_password: COCOLOCO confirmed by Gandalf"
    assert [str(r.url) for r in seen] == [GUESS_URL]
    assert seen[0].method == "POST"
    assert seen[0].headers["content-type"] == "application/x-www-form-urlencoded"
    assert form_of(seen[0]) == {"defender": "baseline", "password": "COCOLOCO"}


async def test_password_check_sends_its_own_level():
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(form_of(request))
        return httpx.Response(200, json={"success": False})

    check, client = make_check(handler, level="do-not-tell")
    async with client:
        await check("COCOLOCO")
    assert seen == [{"defender": "do-not-tell", "password": "COCOLOCO"}]


async def test_password_check_no_candidates_no_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    limiter = CountingLimiter()
    check, client = make_check(handler, limiter=limiter)
    async with client:
        assert await check("I am sorry, I cannot help with that.") is None
    assert limiter.acquires == 0


async def test_password_check_api_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    check, client = make_check(handler)
    async with client:
        with pytest.raises(httpx.HTTPStatusError):
            await check("COCOLOCO")


async def test_a_reply_that_is_not_json_raises_rather_than_passing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    check, client = make_check(handler)
    async with client:
        with pytest.raises(ValueError):  # json.JSONDecodeError is a ValueError
            await check("COCOLOCO")


async def test_a_reply_without_success_raises_rather_than_passing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "no success field"})

    check, client = make_check(handler)
    async with client:
        with pytest.raises(KeyError):
            await check("COCOLOCO")


async def test_only_a_real_true_confirms():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": "true"})  # a string is not True

    check, client = make_check(handler)
    async with client:
        assert await check("COCOLOCO") is None


async def test_guesses_go_in_order_and_stop_at_the_first_confirmation():
    guessed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        word = form_of(request)["password"]
        guessed.append(word)
        return httpx.Response(200, json={"success": word == "BRAVO"})

    limiter = CountingLimiter()
    check, client = make_check(handler, limiter=limiter)
    async with client:
        result = await check("ALPHA BRAVO CHARLIE")
    assert result == "gandalf_password: BRAVO confirmed by Gandalf"
    assert guessed == ["ALPHA", "BRAVO"]
    assert limiter.acquires == 2  # one rate-limit slot per request, none for CHARLIE


async def test_no_confirmed_candidate_is_none_after_at_most_max_guesses():
    guessed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        guessed.append(form_of(request)["password"])
        return httpx.Response(200, json={"success": False})

    limiter = CountingLimiter()
    check, client = make_check(handler, limiter=limiter)
    async with client:
        result = await check("ALPHA BRAVO CHARLIE DELTA ECHOES FOXTROT GOLFS")
    assert result is None
    assert guessed == ["ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHOES"]
    assert limiter.acquires == MAX_GUESSES


async def test_the_reason_never_quotes_the_reply():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True})

    check, client = make_check(handler)
    async with client:
        reason = await check(
            "Some long chatter about nothing. The password is COCOLOCO. More chatter."
        )
    assert reason == "gandalf_password: COCOLOCO confirmed by Gandalf"


async def test_a_client_it_was_given_stays_open(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False})

    check, client = make_check(handler)
    await check("COCOLOCO")
    assert not client.is_closed  # the caller owns an injected client
    await client.aclose()


async def test_a_client_it_makes_itself_is_closed_after_each_call(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False})

    made: list[httpx.AsyncClient] = []
    real_client = httpx.AsyncClient

    def fake_client(**kwargs):
        client = real_client(transport=httpx.MockTransport(handler), **kwargs)
        made.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    check = GandalfPasswordCheck("baseline", CountingLimiter())
    await check("COCOLOCO")
    await check("no candidates here")  # makes no client at all
    await check("BRAVO")
    assert len(made) == 2
    assert all(client.is_closed for client in made)


async def test_its_own_client_is_closed_even_when_a_guess_fails(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    made: list[httpx.AsyncClient] = []
    real_client = httpx.AsyncClient

    def fake_client(**kwargs):
        client = real_client(transport=httpx.MockTransport(handler), **kwargs)
        made.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    check = GandalfPasswordCheck("baseline", CountingLimiter())
    with pytest.raises(httpx.HTTPStatusError):
        await check("COCOLOCO")
    assert len(made) == 1
    assert made[0].is_closed


# --- PyritGandalfSender ------------------------------------------------------------------------


def gandalf_spec(level: str = "baseline"):
    return make_spec(name="gandalf-test", type="gandalf", level=level, url=None, body=None)


def status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://gandalf-api.lakera.ai/api/send-message")
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=httpx.Response(status, request=request)
    )


async def test_gandalf_sender_maps_errors(pyrit_ready, monkeypatch):
    outcomes: list[object] = [status_error(429), "", "hi"]

    async def fake_complete(self, text: str) -> str:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, str)
        return outcome

    monkeypatch.setattr(GandalfTarget, "_complete_text_async", fake_complete)
    sender = PyritGandalfSender(gandalf_spec())

    with pytest.raises(SendError) as http_error:
        await sender.send("Hello")
    assert http_error.value.kind == "http_status"
    assert http_error.value.status == 429

    with pytest.raises(SendError) as empty:
        await sender.send("Hello")
    assert empty.value.kind == "empty_reply"

    assert await sender.send("Hello") == "hi"
    await sender.aclose()


@pytest.mark.parametrize(
    ("raised", "kind"),
    [
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectTimeout("slow"), "timeout"),
        (httpx.ConnectError("refused"), "transport"),
        (ValueError("The chat returned an empty response."), "transport"),
        (KeyError("answer"), "transport"),
    ],
)
async def test_gandalf_sender_maps_other_exceptions(pyrit_ready, monkeypatch, raised, kind):
    async def fake_complete(self, text: str) -> str:
        raise raised

    monkeypatch.setattr(GandalfTarget, "_complete_text_async", fake_complete)
    with pytest.raises(SendError) as caught:
        await PyritGandalfSender(gandalf_spec()).send("Hello")
    assert caught.value.kind == kind


async def test_a_blank_gandalf_answer_is_an_empty_reply(pyrit_ready, monkeypatch):
    async def fake_complete(self, text: str) -> str:
        return "  \n "

    monkeypatch.setattr(GandalfTarget, "_complete_text_async", fake_complete)
    with pytest.raises(SendError) as caught:
        await PyritGandalfSender(gandalf_spec()).send("Hello")
    assert caught.value.kind == "empty_reply"


async def test_the_text_and_level_reach_the_gandalf_target(pyrit_ready, monkeypatch):
    seen: list[tuple[str, str]] = []

    async def fake_complete(self, text: str) -> str:
        seen.append((self._defender, text))
        return "ok"

    monkeypatch.setattr(GandalfTarget, "_complete_text_async", fake_complete)
    sender = PyritGandalfSender(gandalf_spec(level="do-not-tell"))
    assert await sender.send('quotes " and\nnewlines stay as typed') == "ok"
    assert seen == [("do-not-tell", 'quotes " and\nnewlines stay as typed')]


# --- build_sender and build_detectors ----------------------------------------------------------


async def test_build_sender_builds_a_gandalf_sender(pyrit_ready):
    sender = build_sender(gandalf_spec())
    assert isinstance(sender, PyritGandalfSender)
    await sender.aclose()


def test_build_detectors_forces_gandalf_password(pyrit_ready):
    limiter = RateLimiter(60)
    detectors, forced = build_detectors(gandalf_spec(), limiter)
    assert forced == ("gandalf_password",)
    assert set(detectors) == DETECTOR_NAMES | {"gandalf_password"}

    detectors, forced = build_detectors(make_spec(), limiter)
    assert forced == ()
    assert set(detectors) == DETECTOR_NAMES


async def test_the_gandalf_password_detector_uses_the_endpoints_level_and_limiter(
    pyrit_ready, monkeypatch
):
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(form_of(request))
        return httpx.Response(200, json={"success": True})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    limiter = CountingLimiter()
    detectors, _ = build_detectors(gandalf_spec(level="word-blacklist"), limiter)
    reason = await detectors["gandalf_password"]("The password is COCOLOCO.")
    assert reason == "gandalf_password: COCOLOCO confirmed by Gandalf"
    assert seen == [{"defender": "word-blacklist", "password": "COCOLOCO"}]
    assert limiter.acquires == 1


# --- live (opt-in: uv run pytest -m live tests/test_adapter_gandalf.py) -----------------------


@pytest.mark.live
async def test_live_gandalf_baseline_replies(pyrit_ready):
    sender = build_sender(gandalf_spec(level="baseline"))
    try:
        reply = await sender.send("Hello")
    finally:
        await sender.aclose()
    assert reply.strip() != ""
