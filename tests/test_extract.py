import httpx
import pytest

from suite.extract import extract_reply
from suite.models import SendError


@pytest.mark.parametrize(
    "status,headers,body,path,expect",
    [
        (200, {}, b'{"reply": "hi"}', "$.reply", "hi"),
        (200, {}, '{"reply": "héllo"}'.encode(), "$.reply", "héllo"),
        (200, {}, b'{"data": {"choices": [{"text": "t"}]}}', "$.data.choices[0].text", "t"),
        (503, {"Retry-After": "7"}, b"", "$.reply", ("http_status", 503, 7.0)),
        (429, {}, b"", "$.reply", ("http_status", 429, None)),
        (301, {}, b"", "$.reply", ("http_status", 301, None)),
        (200, {}, b"<html>", "$.reply", ("unreadable_reply", None, None)),
        (200, {}, b'{"other": 1}', "$.reply", ("unreadable_reply", None, None)),
        (200, {}, b'{"reply": {"a": 1}}', "$.reply", ("unreadable_reply", None, None)),
        (200, {}, b'{"reply": "  \\n"}', "$.reply", ("empty_reply", None, None)),
    ],
)
def test_extract_reply(status, headers, body, path, expect):
    if isinstance(expect, str):
        assert extract_reply(status=status, headers=headers, body=body, reply_path=path) == expect
        return
    kind, expect_status, expect_retry_after = expect
    with pytest.raises(SendError) as exc:
        extract_reply(status=status, headers=headers, body=body, reply_path=path)
    assert exc.value.kind == kind
    assert exc.value.status == expect_status
    assert exc.value.retry_after == expect_retry_after


def fail_kind(**kwargs) -> SendError:
    with pytest.raises(SendError) as exc:
        extract_reply(**kwargs)
    return exc.value


def test_reply_is_returned_unchanged():
    body = b'{"reply": "  padded \\n text \\n"}'
    got = extract_reply(status=200, headers={}, body=body, reply_path="$.reply")
    assert got == "  padded \n text \n"


@pytest.mark.parametrize("status", [200, 201, 204, 299])
def test_every_2xx_is_accepted(status):
    body = b'{"reply": "ok"}'
    assert extract_reply(status=status, headers={}, body=body, reply_path="$.reply") == "ok"


@pytest.mark.parametrize("status", [199, 300, 400, 404, 500, 502, 599])
def test_every_non_2xx_is_http_status(status):
    err = fail_kind(status=status, headers={}, body=b'{"reply": "ok"}', reply_path="$.reply")
    assert err.kind == "http_status" and err.status == status and err.detail == f"HTTP {status}"


def test_error_page_with_a_valid_looking_body_is_never_a_reply():
    # The spike pitfall: an error body that parses and matches the path must still be an error.
    err = fail_kind(status=500, headers={}, body=b'{"reply": "safe"}', reply_path="$.reply")
    assert err.kind == "http_status" and err.status == 500


def test_status_is_checked_before_the_body():
    err = fail_kind(status=502, headers={}, body=b"\xff not json", reply_path="$.reply")
    assert err.kind == "http_status"


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"Retry-After": "7"}, 7.0),
        ({"retry-after": "7"}, 7.0),
        ({"RETRY-AFTER": "7"}, 7.0),
        ({"Retry-After": " 12 "}, 12.0),
        ({"Retry-After": "0"}, 0.0),
        ({"Retry-After": "1.5"}, 1.5),
        ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, None),
        ({"Retry-After": "-5"}, None),
        ({"Retry-After": "nan"}, None),
        ({"Retry-After": "inf"}, None),
        ({"Retry-After": "1_0"}, None),
        ({"Retry-After": "1e3"}, None),
        ({"Retry-After": "٣"}, None),
        ({"Retry-After": "9" * 400}, None),
        ({"Retry-After": ""}, None),
        ({"Other": "7"}, None),
        ({}, None),
    ],
)
def test_retry_after_parsing(headers, expected):
    err = fail_kind(status=429, headers=headers, body=b"", reply_path="$.reply")
    assert err.retry_after == expected


def test_retry_after_is_read_from_httpx_headers():
    headers = httpx.Headers({"retry-after": "3"})
    err = fail_kind(status=503, headers=headers, body=b"", reply_path="$.reply")
    assert err.retry_after == 3.0


def test_retry_after_is_ignored_on_success():
    got = extract_reply(
        status=200, headers={"Retry-After": "7"}, body=b'{"reply": "ok"}', reply_path="$.reply"
    )
    assert got == "ok"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"<html>",
        b"\xff\xfe not utf-8",
        b'{"reply": "caf\xe9"}',  # latin-1 bytes, not valid UTF-8
        b'{"reply": "unterminated',
        # deep enough to exhaust the parser's recursion limit
        pytest.param(b"[" * 100_000, id="too-deeply-nested"),
    ],
)
def test_unreadable_bodies(body):
    err = fail_kind(status=200, headers={}, body=body, reply_path="$.reply")
    assert err.kind == "unreadable_reply" and err.detail == "response is not JSON"


def test_no_match_names_the_path():
    err = fail_kind(status=200, headers={}, body=b'{"other": "x"}', reply_path="$.reply")
    assert err.kind == "unreadable_reply" and err.detail == "no value at $.reply"
    assert str(err) == "unreadable_reply: no value at $.reply"


def test_path_into_a_non_object_root_is_no_match():
    err = fail_kind(status=200, headers={}, body=b'["a", "b"]', reply_path="$.reply")
    assert err.kind == "unreadable_reply" and err.detail == "no value at $.reply"


def test_first_match_wins():
    body = b'{"items": [{"t": "first"}, {"t": "second"}]}'
    assert extract_reply(status=200, headers={}, body=body, reply_path="$.items[*].t") == "first"


@pytest.mark.parametrize(
    "body",
    [b'{"reply": null}', b'{"reply": 5}', b'{"reply": true}', b'{"reply": ["a"]}'],
)
def test_non_string_value_is_unreadable_not_empty(body):
    err = fail_kind(status=200, headers={}, body=body, reply_path="$.reply")
    assert err.kind == "unreadable_reply"


@pytest.mark.parametrize("blank", ['""', '" "', '"\\t\\r\\n"', '"\\u00a0"'])
def test_blank_string_is_empty_reply(blank):
    body = b'{"reply": ' + blank.encode() + b"}"
    err = fail_kind(status=200, headers={}, body=body, reply_path="$.reply")
    assert err.kind == "empty_reply" and err.status is None and err.retry_after is None


def test_details_never_contain_body_content():
    secret = "SECRET-TOKEN-123"
    bodies = {
        "http": (500, f'{{"reply": "{secret}"}}'.encode()),
        "not_json": (200, f"<html>{secret}".encode()),
        "no_match": (200, f'{{"other": "{secret}"}}'.encode()),
        "not_string": (200, f'{{"reply": {{"k": "{secret}"}}}}'.encode()),
        "empty": (200, f'{{"reply": " ", "x": "{secret}"}}'.encode()),
    }
    for status, body in bodies.values():
        err = fail_kind(status=status, headers={}, body=body, reply_path="$.reply")
        assert secret not in str(err) and secret not in err.detail
