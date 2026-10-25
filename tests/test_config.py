import json
from pathlib import Path

import pytest

from suite.config import GANDALF_LEVELS, EndpointSpec, Limits, load_endpoint, masked
from suite.models import ConfigError
from tests.factories import make_spec

MINIMAL = """\
name: lab-bot
type: http
url: http://127.0.0.1:8000/chat
body:
  message: "{prompt}"
"""

WITH_TOKEN = """\
name: lab-bot
type: http
url: http://127.0.0.1:8000/chat
headers:
  Authorization: "Bearer ${TOK}"
body:
  message: "{prompt}"
"""


def write(tmp_path: Path, text: str, name: str = "endpoint.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_http_endpoint_with_defaults(tmp_path):
    spec = load_endpoint(write(tmp_path, MINIMAL), env={})
    assert spec.limits == Limits(
        concurrency=10,
        requests_per_minute=120,
        timeout_s=60,
        max_requests=1000,
        max_retries=2,
        abort_after_errors=10,
    )
    assert (spec.reply, spec.redact, spec.method) == ("$.reply", True, "POST")


def test_interpolates_env_vars(tmp_path):
    p = write(tmp_path, WITH_TOKEN)
    assert load_endpoint(p, env={"TOK": "s3cret"}).headers["Authorization"] == "Bearer s3cret"


def test_interpolates_nested_body_values_and_keeps_prompt_placeholder(tmp_path):
    p = write(
        tmp_path,
        """\
name: lab-bot
type: http
url: http://127.0.0.1:8000/chat
body:
  user: "${WHO}"
  messages:
    - role: user
      content: "{prompt}"
""",
    )
    spec = load_endpoint(p, env={"WHO": "alice"})
    assert spec.body == {
        "user": "alice",
        "messages": [{"role": "user", "content": "{prompt}"}],
    }


def test_missing_env_var_names_variable_not_value(tmp_path):
    p = write(tmp_path, WITH_TOKEN)
    with pytest.raises(ConfigError, match=r"TOK") as e:
        load_endpoint(p, env={})
    assert str(p) in str(e.value)


def test_missing_env_vars_are_all_reported(tmp_path):
    p = write(tmp_path, WITH_TOKEN.replace("${TOK}", "${A_ONE}-${B_TWO}"))
    with pytest.raises(ConfigError) as e:
        load_endpoint(p, env={})
    assert "A_ONE" in str(e.value)
    assert "B_TWO" in str(e.value)


def test_dotenv_file_is_read(tmp_path, monkeypatch):
    p = write(tmp_path, WITH_TOKEN)
    (tmp_path / ".env").write_text("TOK=fromfile\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TOK", raising=False)
    assert load_endpoint(p).headers["Authorization"] == "Bearer fromfile"


def test_process_environment_overrides_dotenv_file(tmp_path, monkeypatch):
    p = write(tmp_path, WITH_TOKEN)
    (tmp_path / ".env").write_text("TOK=fromfile\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TOK", "fromenv")
    assert load_endpoint(p).headers["Authorization"] == "Bearer fromenv"


def test_secret_value_never_appears_in_validation_error(tmp_path):
    # pydantic's own message would echo the rejected input ("input_value='s3cret'").
    p = write(tmp_path, MINIMAL.replace("http://127.0.0.1:8000/chat", "${TOK}"))
    with pytest.raises(ConfigError, match="url") as e:
        load_endpoint(p, env={"TOK": "s3cret"})
    assert str(e.value).startswith(str(p))
    assert "s3cret" not in str(e.value)
    assert e.value.__cause__ is None
    assert e.value.__suppress_context__


@pytest.mark.parametrize("body", [{"message": "hi"}, {"a": "{prompt}", "b": "{prompt}"}])
def test_body_needs_exactly_one_prompt_placeholder(tmp_path, body):
    lines = "".join(f'  {k}: "{v}"\n' for k, v in body.items())
    p = write(tmp_path, f"name: b\ntype: http\nurl: http://x.test/c\nbody:\n{lines}")
    with pytest.raises(ConfigError, match=r"{prompt}") as e:
        load_endpoint(p, env={})
    assert str(e.value).startswith(str(p))


def test_body_rejects_placeholder_repeated_in_one_value(tmp_path):
    p = write(
        tmp_path,
        'name: b\ntype: http\nurl: http://x.test/c\nbody:\n  m: "{prompt} and {prompt}"\n',
    )
    with pytest.raises(ConfigError, match=r"{prompt}"):
        load_endpoint(p, env={})


def test_http_requires_url_and_body(tmp_path):
    no_url = write(tmp_path, 'name: b\ntype: http\nbody: {m: "{prompt}"}\n', "a.yaml")
    with pytest.raises(ConfigError, match="url"):
        load_endpoint(no_url, env={})
    no_body = write(tmp_path, "name: b\ntype: http\nurl: http://x.test/c\n", "b.yaml")
    with pytest.raises(ConfigError, match="body"):
        load_endpoint(no_body, env={})


@pytest.mark.parametrize("url", ["ftp://x.test/c", "x.test/chat", "file:///etc/passwd"])
def test_url_must_be_http_or_https(tmp_path, url):
    p = write(tmp_path, f'name: b\ntype: http\nurl: "{url}"\nbody: {{m: "{{prompt}}"}}\n')
    with pytest.raises(ConfigError, match="url"):
        load_endpoint(p, env={})


def test_gandalf_needs_known_level(tmp_path):
    bad = write(tmp_path, "name: g\ntype: gandalf\nlevel: nope\n", "bad.yaml")
    with pytest.raises(ConfigError, match="level"):
        load_endpoint(bad, env={})
    missing = write(tmp_path, "name: g\ntype: gandalf\n", "missing.yaml")
    with pytest.raises(ConfigError, match="level"):
        load_endpoint(missing, env={})
    ok = write(tmp_path, "name: g\ntype: gandalf\nlevel: baseline\n", "ok.yaml")
    spec = load_endpoint(ok, env={})
    assert (spec.level, spec.url, spec.body) == ("baseline", None, None)


def test_gandalf_levels_are_the_ten_known_levels():
    assert len(GANDALF_LEVELS) == 10
    assert GANDALF_LEVELS[0] == "baseline"
    assert len(set(GANDALF_LEVELS)) == 10


def test_unknown_key_rejected(tmp_path):
    p = write(tmp_path, MINIMAL + "limits: {concurency: 5}\n")
    with pytest.raises(ConfigError, match="concurency"):
        load_endpoint(p, env={})


def test_unknown_top_level_key_rejected(tmp_path):
    p = write(tmp_path, MINIMAL + "colour: red\n")
    with pytest.raises(ConfigError, match="colour"):
        load_endpoint(p, env={})


@pytest.mark.parametrize(
    "limit",
    [
        "concurrency: 0",
        "concurrency: 201",
        "requests_per_minute: 0",
        "timeout_s: 0",
        "max_requests: 0",
        "max_retries: 6",
        "max_retries: -1",
        "abort_after_errors: 0",
    ],
)
def test_limits_are_range_checked(tmp_path, limit):
    p = write(tmp_path, MINIMAL + f"limits: {{{limit}}}\n")
    with pytest.raises(ConfigError, match=limit.split(":")[0]):
        load_endpoint(p, env={})


@pytest.mark.parametrize("name", ["", "Lab", "-lab", "has space", "a" * 49])
def test_name_pattern_enforced(tmp_path, name):
    p = write(tmp_path, MINIMAL.replace("name: lab-bot", f'name: "{name}"'))
    with pytest.raises(ConfigError, match="name"):
        load_endpoint(p, env={})


def test_authorized_optional_at_load(tmp_path):
    spec = load_endpoint(write(tmp_path, MINIMAL), env={})
    assert spec.authorized is None


def test_reply_path_must_parse(tmp_path):
    p = write(tmp_path, MINIMAL + 'reply: "$..["\n')
    with pytest.raises(ConfigError, match="reply"):
        load_endpoint(p, env={})


def test_invalid_yaml_is_a_config_error_starting_with_path(tmp_path):
    p = write(tmp_path, "name: [unclosed\n")
    with pytest.raises(ConfigError) as e:
        load_endpoint(p, env={})
    assert str(e.value).startswith(str(p))


@pytest.mark.parametrize("text", ["", "- just\n- a list\n", "plain string\n"])
def test_top_level_must_be_a_mapping(tmp_path, text):
    p = write(tmp_path, text)
    with pytest.raises(ConfigError) as e:
        load_endpoint(p, env={})
    assert str(e.value).startswith(str(p))


def test_missing_file_is_a_config_error(tmp_path):
    p = tmp_path / "nope.yaml"
    with pytest.raises(ConfigError) as e:
        load_endpoint(p, env={})
    assert str(e.value).startswith(str(p))


def test_masked_hides_header_values():
    spec = make_spec(headers={"Authorization": "Bearer s3cret"})
    assert masked(spec)["headers"] == {"Authorization": "***"}
    assert "s3cret" not in json.dumps(masked(spec))


def test_masked_does_not_mutate_the_spec():
    spec = make_spec(headers={"Authorization": "Bearer s3cret"})
    masked(spec)
    assert spec.headers == {"Authorization": "Bearer s3cret"}


SECRET_IN_URL_AND_BODY = """name: lab-bot
type: http
url: "http://h/chat?key=${KEY}"
body:
  api_key: "${KEY}"
  message: "{prompt}"
"""


def test_masked_hides_interpolated_values_in_url_and_body(tmp_path):
    spec = load_endpoint(write(tmp_path, SECRET_IN_URL_AND_BODY), env={"KEY": "s3cretvalue"})
    out = masked(spec)
    assert "s3cretvalue" not in json.dumps(out)
    assert out["url"] == "http://h/chat?key=***"
    assert out["body"] == {"api_key": "***", "message": "{prompt}"}
    # the spec itself still holds the real values for the engine to send
    assert spec.url == "http://h/chat?key=s3cretvalue"
    assert spec.body == {"api_key": "s3cretvalue", "message": "{prompt}"}


def test_masked_hides_interpolated_values_in_nested_lists(tmp_path):
    p = write(
        tmp_path,
        """name: lab-bot
type: http
url: http://h/chat
body:
  auth: {tokens: ["${KEY}", "x-${KEY}"]}
  message: "{prompt}"
""",
    )
    spec = load_endpoint(p, env={"KEY": "s3cretvalue"})
    out = masked(spec)
    assert "s3cretvalue" not in json.dumps(out)
    assert out["body"]["auth"] == {"tokens": ["***", "x-***"]}


def test_masked_hides_the_longer_secret_when_one_contains_the_other(tmp_path):
    p = write(tmp_path, SECRET_IN_URL_AND_BODY.replace('"{prompt}"', '"${LONG}{prompt}"'))
    spec = load_endpoint(p, env={"KEY": "abc", "LONG": "abcdef"})
    assert "def" not in json.dumps(masked(spec))


def test_masked_leaves_a_spec_without_interpolation_unchanged_apart_from_headers():
    spec = make_spec(headers={"X": "v"})
    assert masked(spec) == {**spec.model_dump(), "headers": {"X": "***"}}


@pytest.mark.parametrize("ref", ["${token}", "${Token}", "${MY-TOKEN}", "${}", "${ TOK }"])
def test_leftover_placeholder_rejected(tmp_path, ref):
    p = write(tmp_path, WITH_TOKEN.replace("${TOK}", ref))
    with pytest.raises(ConfigError, match=r"headers\.Authorization") as e:
        load_endpoint(p, env={"TOK": "s3cret", "token": "s3cret"})
    assert str(e.value).startswith(str(p))
    assert "UPPER_CASE" in str(e.value)
    assert "s3cret" not in str(e.value)


def test_leftover_placeholder_names_nested_body_path(tmp_path):
    p = write(
        tmp_path,
        """name: lab-bot
type: http
url: http://h/chat
body:
  messages:
    - content: "{prompt}"
      key: "${lower}"
""",
    )
    with pytest.raises(ConfigError, match=r"body\.messages\[0\]\.key"):
        load_endpoint(p, env={})


def test_secret_value_containing_dollar_brace_is_not_mistaken_for_a_leftover(tmp_path):
    p = write(tmp_path, WITH_TOKEN)
    spec = load_endpoint(p, env={"TOK": "pa${ss"})
    assert spec.headers["Authorization"] == "Bearer pa${ss"


def test_make_spec_builds_valid_http_spec_and_merges_limits():
    spec = make_spec(limits=dict(concurrency=2))
    assert isinstance(spec, EndpointSpec)
    assert (spec.name, spec.authorized, spec.url) == ("t", "test", "http://127.0.0.1:9/chat")
    assert spec.body == {"message": "{prompt}"}
    assert spec.limits == Limits(concurrency=2)


# --- unsafe headers and urls ----------------------------------------------------------------
# PyRIT builds the request URL from a Host header and copies header lines straight out of a raw
# request, so a spec must not be able to redirect a request, inject a header, or receive a prompt
# anywhere but the body.


def header_yaml(headers: str, url: str = "http://x.test/c") -> str:
    return f'name: b\ntype: http\nurl: "{url}"\nheaders:\n{headers}body: {{m: "{{prompt}}"}}\n'


def header_line(name: str, value: str) -> str:
    """One YAML header line; JSON quoting writes control characters as escapes YAML decodes."""
    return f"  {json.dumps(name)}: {json.dumps(value)}\n"


@pytest.mark.parametrize("name", ["Host", "host", "HOST", "Content-Length", "content-length"])
def test_headers_cannot_set_host_or_content_length(tmp_path, name):
    p = write(tmp_path, header_yaml(header_line(name, "v")))
    with pytest.raises(ConfigError, match="headers") as e:
        load_endpoint(p, env={})
    assert str(e.value).startswith(str(p))
    assert "cannot be set" in str(e.value)


@pytest.mark.parametrize(
    "name",
    ["Host ", " host", "Host: evil.test", "X:Y", "X Y", "X-A\nB", "X-A\r\nHost", "X-{PROMPT}", ""],
)
def test_header_names_must_be_plain_tokens(tmp_path, name):
    # PyRIT strips and splits at the first colon, so "Host :" would otherwise override Host.
    p = write(tmp_path, header_yaml(header_line(name, "v")))
    with pytest.raises(ConfigError, match="headers") as e:
        load_endpoint(p, env={})
    assert str(e.value).startswith(str(p))
    assert "header name" in str(e.value)


@pytest.mark.parametrize("value", ["a\nb", "a\rb", "a\r\nX-Evil: 1"])
def test_header_values_reject_line_breaks(tmp_path, value):
    p = write(tmp_path, header_yaml(header_line("X-Key", value)))
    with pytest.raises(ConfigError, match="headers") as e:
        load_endpoint(p, env={})
    assert str(e.value).startswith(str(p))
    assert "X-Key" in str(e.value)
    assert "CR or LF" in str(e.value)


def test_line_break_error_never_echoes_the_secret_value(tmp_path):
    p = write(tmp_path, WITH_TOKEN)
    with pytest.raises(ConfigError, match="headers") as e:
        load_endpoint(p, env={"TOK": "s3cret\r\nX-Evil: 1"})
    assert str(e.value).startswith(str(p))
    assert "s3cret" not in str(e.value)
    assert "X-Evil" not in str(e.value)
    assert e.value.__cause__ is None


def test_prompt_marker_is_rejected_in_a_header_value(tmp_path):
    p = write(tmp_path, header_yaml('  X-Key: "id-{PROMPT}"\n'))
    with pytest.raises(ConfigError, match="headers") as e:
        load_endpoint(p, env={})
    assert str(e.value).startswith(str(p))
    assert "{PROMPT}" in str(e.value)
    assert "X-Key" in str(e.value)


@pytest.mark.parametrize("url", ["http://x.test/{PROMPT}", "http://x.test/c?q={PROMPT}"])
def test_prompt_marker_is_rejected_in_the_url(tmp_path, url):
    p = write(tmp_path, header_yaml("  X-Key: v\n", url=url))
    with pytest.raises(ConfigError, match="url") as e:
        load_endpoint(p, env={})
    assert str(e.value).startswith(str(p))
    assert "{PROMPT}" in str(e.value)


def test_ordinary_headers_are_accepted(tmp_path):
    p = write(
        tmp_path,
        header_yaml(
            '  Authorization: "Bearer ${TOK}"\n  X-Api-Key: k\n  Cookie: a=b\n'
            "  Content-Type: application/json\n  x_odd.name-1: v\n"
        ),
    )
    spec = load_endpoint(p, env={"TOK": "t"})
    assert list(spec.headers) == [
        "Authorization",
        "X-Api-Key",
        "Cookie",
        "Content-Type",
        "x_odd.name-1",
    ]
