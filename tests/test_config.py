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


def test_make_spec_builds_valid_http_spec_and_merges_limits():
    spec = make_spec(limits=dict(concurrency=2))
    assert isinstance(spec, EndpointSpec)
    assert (spec.name, spec.authorized, spec.url) == ("t", "test", "http://127.0.0.1:9/chat")
    assert spec.body == {"message": "{prompt}"}
    assert spec.limits == Limits(concurrency=2)
