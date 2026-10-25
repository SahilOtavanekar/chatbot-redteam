import pytest

from suite.config import EndpointSpec
from suite.models import Attempt
from suite.planner import PreflightReport, build_attempts, format_preflight, preflight
from tests.factories import make_pack, make_prompt, make_spec


def pack_of(count: int):
    """A pack that expands to exactly `count` attempts (a prompt repeats at most 20 times)."""
    full, rest = divmod(count, 20)
    sizes = [20] * full + ([rest] if rest else [])
    return make_pack(*(make_prompt(f"P{i}", repeat=size) for i, size in enumerate(sizes)))


def test_build_attempts_expands_repeat():
    pack = make_pack(make_prompt("A", repeat=2, text="ta"), make_prompt("B"))
    atts = build_attempts(pack)
    assert [a.attempt_id for a in atts] == ["A#1", "A#2", "B#1"] and atts[0].text == "ta"


def test_build_attempts_fields_and_pack_order():
    pack = make_pack(
        make_prompt("B", repeat=2, text="tb"),
        make_prompt("A", text="ta"),
    )
    assert build_attempts(pack) == [
        Attempt(attempt_id="B#1", prompt_id="B", repeat_index=1, text="tb"),
        Attempt(attempt_id="B#2", prompt_id="B", repeat_index=2, text="tb"),
        Attempt(attempt_id="A#1", prompt_id="A", repeat_index=1, text="ta"),
    ]


def test_build_attempts_keeps_prompt_text_verbatim():
    text = "  keep  the   spacing\nand the newline  "
    assert build_attempts(make_pack(make_prompt("A", text=text)))[0].text == text


def test_preflight_ok():
    pack = make_pack(make_prompt("A", repeat=2), make_prompt("B"))
    report = preflight(make_spec(limits=dict(requests_per_minute=60, concurrency=4)), pack)
    assert report.problems == ()
    assert report.ok
    assert report.prompts == 2
    assert report.attempts == 3
    assert report.max_requests == 1000
    assert report.requests_per_minute == 60
    assert report.concurrency == 4
    assert report.authorized == "test"
    assert report.min_duration_s == 0.0


def test_preflight_flags_missing_authorization():
    report = preflight(make_spec(authorized=None), make_pack(make_prompt("A")))
    assert not report.ok
    assert report.authorized is None
    assert report.problems == (
        "endpoint 't' has no 'authorized' field "
        "— add the owner's go-ahead reference before running",
    )


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_preflight_treats_blank_authorization_as_missing(blank):
    report = preflight(make_spec(authorized=blank), make_pack(make_prompt("A")))
    assert report.authorized is None
    assert len(report.problems) == 1 and "authorized" in report.problems[0]


def test_preflight_flags_budget():
    report = preflight(make_spec(limits=dict(max_requests=2)), pack_of(3))
    assert report.problems == ("plan needs 3 requests but max_requests is 2",)
    assert not report.ok


def test_preflight_budget_exactly_met_is_ok():
    assert preflight(make_spec(limits=dict(max_requests=3)), pack_of(3)).ok


def test_preflight_lists_every_problem():
    report = preflight(make_spec(authorized=None, limits=dict(max_requests=2)), pack_of(3))
    assert len(report.problems) == 2
    assert "authorized" in report.problems[0]
    assert "max_requests" in report.problems[1]


@pytest.mark.parametrize("attempts,expected", [(120, 0.0), (121, 60.0), (300, 120.0)])
def test_min_duration(attempts, expected):
    spec = make_spec(limits=dict(requests_per_minute=120, max_requests=1000))
    assert preflight(spec, pack_of(attempts)).min_duration_s == expected


def test_pack_of_expands_to_the_requested_count():
    assert len(build_attempts(pack_of(300))) == 300
    assert len(build_attempts(pack_of(121))) == 121


def test_min_duration_of_an_empty_plan_is_zero():
    assert preflight(make_spec(), make_pack()).min_duration_s == 0.0


def test_report_is_frozen():
    report = preflight(make_spec(), pack_of(1))
    with pytest.raises(AttributeError):
        report.attempts = 5  # type: ignore[misc]


def lines_of(report: PreflightReport, spec: EndpointSpec) -> list[str]:
    return format_preflight(report, spec).splitlines()


def test_format_preflight_http_lines_in_order():
    spec = make_spec(name="lab-bot", limits=dict(requests_per_minute=60, concurrency=4))
    pack = make_pack(make_prompt("A", repeat=2), make_prompt("B"))
    assert lines_of(preflight(spec, pack), spec) == [
        "endpoint: lab-bot",
        "type: http",
        "url: http://127.0.0.1:9/chat",
        "prompts: 2",
        "attempts: 3",
        "max_requests: 1000",
        "requests_per_minute: 60",
        "concurrency: 4",
        "min_duration_s: 0.0",
        "authorized: test",
    ]


def test_format_preflight_gandalf_shows_level_not_url():
    spec = EndpointSpec(name="gandalf-1", type="gandalf", authorized="challenge", level="baseline")
    lines = lines_of(preflight(spec, pack_of(1)), spec)
    assert lines[:3] == ["endpoint: gandalf-1", "type: gandalf", "level: baseline"]
    assert not any(line.startswith("url:") for line in lines)


def test_format_preflight_marks_missing_authorization_and_lists_problems():
    spec = make_spec(authorized=None, limits=dict(max_requests=2))
    lines = lines_of(preflight(spec, pack_of(3)), spec)
    assert "authorized: MISSING" in lines
    problems = [line for line in lines if line.startswith("problem: ")]
    assert problems == [f"problem: {p}" for p in preflight(spec, pack_of(3)).problems]
    assert lines[-2:] == problems  # problems come last, one per line


def test_format_preflight_has_no_problem_lines_when_ok():
    spec = make_spec()
    assert not any(
        line.startswith("problem") for line in lines_of(preflight(spec, pack_of(1)), spec)
    )


def test_format_preflight_never_shows_secrets():
    spec = make_spec(headers={"Authorization": "Bearer s3cret"}, authorized=None)
    out = format_preflight(preflight(spec, pack_of(1)), spec)
    assert "s3cret" not in out
    assert "Bearer" not in out and "Authorization" not in out
    assert "problem: " in out


def test_format_preflight_masks_a_secret_interpolated_into_the_url(tmp_path):
    from suite.config import load_endpoint

    path = tmp_path / "bot.yaml"
    path.write_text(
        "name: bot\ntype: http\nauthorized: owner ok\n"
        "url: https://example.test/chat?key=${BOT_KEY}\n"
        "body: {message: '{prompt}'}\n",
        encoding="utf-8",
    )
    spec = load_endpoint(path, env={"BOT_KEY": "k3y-value"})
    out = format_preflight(preflight(spec, pack_of(1)), spec)
    assert "k3y-value" not in out
    assert "url: https://example.test/chat?key=***" in out
