"""Tests for run orchestration: engine -> judge -> evidence -> draft report.

Every test is Executed. All but the last use a FakeSender (no network); the last one drives the
real PyRIT HTTP sender against the local mock bot (tests/mockbot.py) over a real socket.
"""

import asyncio
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from suite import run as run_module
from suite.config import EndpointSpec, load_endpoint, masked
from suite.engine import RateLimiter
from suite.evidence import EvidenceStore, read_run_json
from suite.judge import Detector
from suite.models import ConfigError, SendError, Verdict
from suite.prompts import Pack
from suite.report import build_report_data
from suite.run import execute_run
from tests.factories import make_pack, make_prompt, make_spec
from tests.fakes import FakeSender

CANARY = "SYSTEM-CANARY-7Q4P"
FIXED_NOW = datetime(2026, 10, 24, 9, 30, 0, tzinfo=UTC)
PACK_PATH = Path("prompts") / "t.yaml"
RUN_FILES = {"run.json", "evidence.db", "report.html", "report.md", "matrix.csv", "review.csv"}
SummaryRow = dict[str, int]

# --- helpers ---------------------------------------------------------------------------------


async def reply_ok(text: str, n: int) -> str:
    return "echo: " + text


async def reply_canary_for_leak(text: str, n: int) -> str:
    return f"Sure. {CANARY}" if "leak" in text else "I'm sorry, I can't help with that."


async def always_http_500(text: str, n: int) -> str:
    raise SendError("http_status", "HTTP 500", status=500)


async def go(
    tmp_path: Path,
    sender: FakeSender,
    pack: Pack,
    *,
    spec: EndpointSpec | None = None,
    detectors: Mapping[str, Detector] | None = None,
    forced: tuple[str, ...] = (),
    now: Callable[[], datetime] = lambda: FIXED_NOW,
) -> Path:
    """Run with a fast limiter and fail rather than hang."""
    spec = spec or make_spec()
    async with asyncio.timeout(10):
        return await execute_run(
            spec,
            pack,
            pack_path=PACK_PATH,
            results_dir=tmp_path,
            sender=sender,
            detectors=detectors or {},
            forced=forced,
            limiter=RateLimiter(100_000),
            now=now,
        )


def stored(run_dir: Path) -> dict[str, tuple[Verdict, tuple[str, ...]]]:
    """``attempt_id -> (verdict, reasons)`` as stored in the run's evidence.db."""
    store = EvidenceStore(run_dir / "evidence.db")
    try:
        return {row.attempt_id: (row.verdict, row.reasons) for row in store.attempts()}
    finally:
        store.close()


def only_folder(root: Path) -> Path:
    folders = [p for p in root.iterdir() if p.is_dir()]
    assert len(folders) == 1
    return folders[0]


# --- the run folder --------------------------------------------------------------------------


async def test_run_folder_contents(tmp_path):
    pack = make_pack(
        make_prompt("A", text="leak A", must_not_contain=[CANARY]),
        make_prompt("B", text="plain B", must_not_contain=[CANARY]),
        make_prompt("C", text="no checks C"),
        make_prompt("D", text="leak D, twice", repeat=2, must_not_contain=[CANARY]),
    )
    sender = FakeSender(reply_canary_for_leak)
    run_dir = await go(tmp_path, sender, pack)

    assert run_dir.parent == tmp_path
    assert {p.name for p in run_dir.iterdir()} == RUN_FILES
    assert not sender.closed  # the caller owns the sender

    data = read_run_json(run_dir)
    assert set(data) == {
        "run_id",
        "endpoint",
        "pack",
        "pack_path",
        "started_at",
        "finished_at",
        "status",
        "sent_requests",
        "redact",
        "summary",
    }
    assert data["status"] == "complete"
    assert data["run_id"] == run_dir.name == "20261024-093000-t"
    assert data["pack"] == "t"
    assert data["pack_path"] == str(PACK_PATH)
    assert data["started_at"] == FIXED_NOW.isoformat()
    assert data["finished_at"] == FIXED_NOW.isoformat()
    assert data["redact"] is True
    assert data["sent_requests"] == len(sender.calls) == 5
    # Attempt-level counts (D has two attempts), every verdict key always present.
    assert data["summary"] == {"pass": 1, "fail": 3, "inconclusive": 0, "needs_review": 1}
    assert stored(run_dir)["A#1"][0] is Verdict.FAIL
    assert stored(run_dir)["B#1"][0] is Verdict.PASS
    assert stored(run_dir)["C#1"][0] is Verdict.NEEDS_REVIEW
    assert (run_dir / "review.csv").read_text(encoding="utf-8-sig").count("C#1") == 1


async def test_run_json_records_redact_setting_and_masked_endpoint(tmp_path):
    spec = make_spec(redact=False, headers={"X-Key": "k"})
    run_dir = await go(tmp_path, FakeSender(reply_ok), make_pack(make_prompt()), spec=spec)
    data = read_run_json(run_dir)
    assert data["redact"] is False
    assert data["endpoint"] == masked(spec)
    assert data["endpoint"]["headers"] == {"X-Key": "***"}


async def test_run_json_says_running_before_the_first_send(tmp_path):
    seen: list[tuple[str, object, int, list[str]]] = []

    async def script(text: str, n: int) -> str:
        data = read_run_json(only_folder(tmp_path))
        store = EvidenceStore(only_folder(tmp_path) / "evidence.db")
        try:
            prompt_ids = [p.prompt_id for p in store.prompts()]
        finally:
            store.close()
        seen.append((data["status"], data["finished_at"], data["sent_requests"], prompt_ids))
        return "ok"

    run_dir = await go(tmp_path, FakeSender(script), make_pack(make_prompt("A"), make_prompt("B")))
    assert seen == [("running", None, 0, ["A", "B"])] * 2
    assert read_run_json(run_dir)["status"] == "complete"


async def test_started_and_finished_come_from_the_clock(tmp_path):
    ticks = (FIXED_NOW + timedelta(seconds=n) for n in range(100))
    run_dir = await go(
        tmp_path, FakeSender(reply_ok), make_pack(make_prompt()), now=lambda: next(ticks)
    )
    data = read_run_json(run_dir)
    assert data["started_at"] == FIXED_NOW.isoformat()
    assert data["run_id"] == "20261024-093000-t"  # named from the start time
    assert datetime.fromisoformat(data["finished_at"]) > FIXED_NOW


async def test_same_second_runs_do_not_collide(tmp_path):
    pack = make_pack(make_prompt())
    first = await go(tmp_path, FakeSender(reply_ok), pack)
    second = await go(tmp_path, FakeSender(reply_ok), pack)
    third = await go(tmp_path, FakeSender(reply_ok), pack)

    assert first.name == "20261024-093000-t"
    assert second.name == "20261024-093000-t-2"
    assert third.name == "20261024-093000-t-3"
    for folder in (first, second, third):
        assert read_run_json(folder)["run_id"] == folder.name
        assert {p.name for p in folder.iterdir()} == RUN_FILES


async def test_creates_the_results_folder_if_missing(tmp_path):
    results = tmp_path / "deep" / "results"
    sender = FakeSender(reply_ok)
    async with asyncio.timeout(10):
        run_dir = await execute_run(
            make_spec(),
            make_pack(make_prompt()),
            pack_path=PACK_PATH,
            results_dir=results,
            sender=sender,
            detectors={},
            forced=(),
            limiter=RateLimiter(100_000),
            now=lambda: FIXED_NOW,
        )
    assert run_dir.parent == results
    assert read_run_json(run_dir)["status"] == "complete"


# --- judging is wired in ---------------------------------------------------------------------


async def test_judge_and_forced_detectors_wired(tmp_path):
    async def forced_detector(text: str) -> str | None:
        return "forced_check: reply says boom" if "boom" in text else None

    async def script(text: str, n: int) -> str:
        return {"leak": f"Sure. {CANARY}", "bang": "boom!"}.get(text, "all fine")

    pack = make_pack(
        make_prompt("A", text="leak", must_not_contain=[CANARY]),
        make_prompt("B", text="bang"),  # no checks of its own: only the forced detector can fail it
        make_prompt("C", text="calm"),
    )
    run_dir = await go(
        tmp_path,
        FakeSender(script),
        pack,
        detectors={"forced_check": forced_detector},
        forced=("forced_check",),
    )

    rows = stored(run_dir)
    assert rows["A#1"][0] is Verdict.FAIL
    assert rows["A#1"][1] == (f"contains '{CANARY}' (plain text)",)
    assert rows["B#1"] == (Verdict.FAIL, ("forced_check: reply says boom",))
    assert rows["C#1"][0] is Verdict.PASS  # the forced detector ran and found nothing
    assert read_run_json(run_dir)["summary"] == {
        "pass": 1,
        "fail": 2,
        "inconclusive": 0,
        "needs_review": 0,
    }


async def test_send_errors_are_stored_as_inconclusive(tmp_path):
    async def script(text: str, n: int) -> str:
        if text == "boom":
            raise SendError("http_status", "HTTP 500", status=500)
        return "fine"

    pack = make_pack(
        make_prompt("A", text="boom", must_not_contain=[CANARY]),
        make_prompt("B", text="calm", must_not_contain=[CANARY]),
    )
    run_dir = await go(tmp_path, FakeSender(script), pack)

    assert stored(run_dir)["A#1"][0] is Verdict.INCONCLUSIVE
    assert stored(run_dir)["B#1"][0] is Verdict.PASS
    assert read_run_json(run_dir)["summary"]["inconclusive"] == 1
    assert build_report_data(run_dir, final=False).totals[Verdict.INCONCLUSIVE] == 1


# --- secrets ---------------------------------------------------------------------------------


async def test_secrets_never_written(tmp_path, capsys):  # Review Focus 3
    spec = make_spec(headers={"Authorization": "Bearer s3cret"})
    run_dir = await go(tmp_path, FakeSender(reply_ok), make_pack(make_prompt()), spec=spec)

    assert all(b"s3cret" not in p.read_bytes() for p in run_dir.iterdir())
    assert "s3cret" not in capsys.readouterr().out
    assert read_run_json(run_dir)["endpoint"]["headers"] == {"Authorization": "***"}


async def test_interpolated_secrets_never_written(tmp_path, capsys):
    # masked() must see the spec load_endpoint built: the substituted values live on it only.
    endpoint = tmp_path / "bot.yaml"
    endpoint.write_text(
        "name: t\n"
        "type: http\n"
        "authorized: test\n"
        "url: http://127.0.0.1:9/chat?key=${API_KEY}\n"
        "headers:\n  Authorization: Bearer ${API_KEY}\n"
        'body:\n  message: "{prompt}"\n  token: ${API_KEY}\n',
        encoding="utf-8",
    )
    spec = load_endpoint(endpoint, env={"API_KEY": "hunter2-xyz"})
    results = tmp_path / "results"
    run_dir = await go(results, FakeSender(reply_ok), make_pack(make_prompt()), spec=spec)

    assert all(b"hunter2-xyz" not in p.read_bytes() for p in run_dir.iterdir())
    assert "hunter2-xyz" not in capsys.readouterr().out
    assert read_run_json(run_dir)["endpoint"] == masked(spec)
    assert read_run_json(run_dir)["endpoint"]["url"] == "http://127.0.0.1:9/chat?key=***"


async def test_secret_in_an_error_or_an_echoed_reply_never_reaches_the_run_folder(tmp_path):
    # Review Focus 3: an exception message can quote a header value or url (h11's "Illegal
    # header value b'...'", PyRIT's "Invalid port in HTTP destination: <url>"), and a bot can
    # echo its own credential. The judge sees the raw reply; what is stored and reported never
    # holds the secret.
    secret = "hunter2-SECRET"
    endpoint = tmp_path / "bot.yaml"
    endpoint.write_text(
        "name: t\n"
        "type: http\n"
        "authorized: test\n"
        "url: http://127.0.0.1:9/chat?key=${API_KEY}\n"
        "headers:\n  Authorization: Bearer ${API_KEY}\n"
        'body:\n  message: "{prompt}"\n',
        encoding="utf-8",
    )
    spec = load_endpoint(endpoint, env={"API_KEY": secret})

    async def script(text: str, n: int) -> str:
        if text == "send error":
            raise SendError("transport", f"LocalProtocolError: Illegal header value b'{secret}'")
        if text == "crash":  # not a SendError: the engine's catch-all makes it "transport"
            raise RuntimeError(f"Invalid port in HTTP destination: http://h/c?key={secret}")
        return f"Your key is {secret}; keep it safe."  # the bot echoes its credential

    async def echo_check(reply: str) -> str | None:
        return f"credential_leak: {secret} is in the reply" if secret in reply else None

    pack = make_pack(
        make_prompt("A", text="send error", must_not_contain=[CANARY]),
        make_prompt("B", text="crash", must_not_contain=[CANARY]),
        make_prompt("C", text="echo checked", detectors=["credential_leak"]),
        make_prompt("D", text="echo for review"),  # no checks: the reply goes to review.csv
    )
    run_dir = await go(
        tmp_path / "results",
        FakeSender(script),
        pack,
        spec=spec,
        detectors={"credential_leak": echo_check},
    )

    rows = stored(run_dir)
    assert rows["A#1"] == (
        Verdict.INCONCLUSIVE,
        ("transport: LocalProtocolError: Illegal header value b'***'",),
    )
    assert rows["B#1"] == (
        Verdict.INCONCLUSIVE,
        ("transport: RuntimeError: Invalid port in HTTP destination: http://h/c?key=***",),
    )
    # Judged on the raw reply (the check saw the secret and tripped); stored scrubbed.
    assert rows["C#1"] == (Verdict.FAIL, ("credential_leak: *** is in the reply",))
    assert rows["D#1"][0] is Verdict.NEEDS_REVIEW
    assert "Your key is ***; keep it safe." in (run_dir / "review.csv").read_text(
        encoding="utf-8-sig"
    )
    # Every file of the run folder, evidence.db included, byte for byte.
    assert [p.name for p in run_dir.iterdir() if secret.encode() in p.read_bytes()] == []


async def test_nothing_is_printed(tmp_path, capsys):
    await go(tmp_path, FakeSender(reply_ok), make_pack(make_prompt()))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


# --- aborted and interrupted runs ------------------------------------------------------------


async def test_aborted_run_status(tmp_path):
    spec = make_spec(limits=dict(abort_after_errors=2, concurrency=1))
    pack = make_pack(*(make_prompt(f"P-{i}") for i in range(5)))
    run_dir = await go(tmp_path, FakeSender(always_http_500), pack, spec=spec)

    data = read_run_json(run_dir)
    assert data["status"] == "aborted"
    assert data["sent_requests"] == 2  # the run stopped sending after the second error
    # Every attempt is still stored, the unsent ones as Inconclusive (never Pass).
    assert data["summary"] == {"pass": 0, "fail": 0, "inconclusive": 5, "needs_review": 0}
    assert {p.name for p in run_dir.iterdir()} == RUN_FILES
    assert build_report_data(run_dir, final=False).totals[Verdict.PASS] == 0


async def test_interrupted_run_leaves_usable_folder(tmp_path):  # Review Focus 5
    never = asyncio.Event()

    async def script(text: str, n: int) -> str:
        if text in ("one", "two"):
            return "fine"
        await never.wait()  # the third send hangs until the run is cancelled
        return "unreachable"

    pack = make_pack(
        *(
            make_prompt(name, text=name, must_not_contain=[CANARY])
            for name in ("one", "two", "three", "four")
        )
    )
    sender = FakeSender(script)
    task = asyncio.create_task(
        execute_run(
            make_spec(limits=dict(concurrency=1)),
            pack,
            pack_path=PACK_PATH,
            results_dir=tmp_path,
            sender=sender,
            detectors={},
            forced=(),
            limiter=RateLimiter(100_000),
            now=lambda: FIXED_NOW,
        )
    )
    async with asyncio.timeout(10):
        while len(sender.calls) < 3:  # two outcomes stored, the third send is in flight
            await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    run_dir = only_folder(tmp_path)
    data = read_run_json(run_dir)
    assert data["status"] == "interrupted"
    assert data["sent_requests"] == len(sender.calls) == 3
    assert data["summary"] == {"pass": 2, "fail": 0, "inconclusive": 0, "needs_review": 0}
    assert data["finished_at"] is not None
    assert {p.name for p in run_dir.iterdir()} == RUN_FILES
    assert sender.in_flight == 0  # nothing is left sending in the background
    report = build_report_data(run_dir, final=False)
    assert report.totals[Verdict.INCONCLUSIVE] >= 1  # not_run attempts
    assert report.totals[Verdict.PASS] == 2
    matrix = (run_dir / "matrix.csv").read_text(encoding="utf-8-sig")
    assert matrix.count("not_run: no outcome recorded") == 2  # "three" and "four" were never stored


async def test_failure_while_writing_reports_does_not_mask_the_cancellation(
    tmp_path, monkeypatch, caplog
):
    def broken_reports(run_dir: Path, *, final: bool) -> list[Path]:
        raise ConfigError("cannot write reports")

    monkeypatch.setattr(run_module, "write_reports", broken_reports)
    never = asyncio.Event()

    async def script(text: str, n: int) -> str:
        await never.wait()
        return "unreachable"

    sender = FakeSender(script)
    task = asyncio.create_task(
        execute_run(
            make_spec(),
            make_pack(make_prompt()),
            pack_path=PACK_PATH,
            results_dir=tmp_path,
            sender=sender,
            detectors={},
            forced=(),
            limiter=RateLimiter(100_000),
            now=lambda: FIXED_NOW,
        )
    )
    async with asyncio.timeout(10):
        while not sender.calls:
            await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert read_run_json(only_folder(tmp_path))["status"] == "interrupted"  # written before reports
    assert "cannot write reports" in caplog.text  # the secondary error is logged, not raised


async def test_unexpected_failure_marks_the_run_interrupted_and_is_reraised(tmp_path, monkeypatch):
    async def broken_judge(*args: object, **kwargs: object) -> object:
        raise RuntimeError("judge exploded")

    monkeypatch.setattr(run_module, "judge_outcome", broken_judge)
    with pytest.raises(RuntimeError, match="judge exploded"):
        await go(tmp_path, FakeSender(reply_ok), make_pack(make_prompt(), make_prompt("B")))

    run_dir = only_folder(tmp_path)
    assert read_run_json(run_dir)["status"] == "interrupted"
    assert {p.name for p in run_dir.iterdir()} == RUN_FILES
    assert build_report_data(run_dir, final=False).totals[Verdict.PASS] == 0


async def test_report_failure_after_a_finished_run_is_raised(tmp_path, monkeypatch):
    def broken_reports(run_dir: Path, *, final: bool) -> list[Path]:
        raise ConfigError("cannot write reports")

    monkeypatch.setattr(run_module, "write_reports", broken_reports)
    with pytest.raises(ConfigError, match="cannot write reports"):
        await go(tmp_path, FakeSender(reply_ok), make_pack(make_prompt()))

    # The evidence and the final run.json are already safe on disk.
    data = read_run_json(only_folder(tmp_path))
    assert data["status"] == "complete"
    assert data["summary"]["needs_review"] == 1


async def test_limits_come_from_the_spec(tmp_path):
    spec = make_spec(limits=dict(concurrency=2))
    release = asyncio.Event()

    async def script(text: str, n: int) -> str:
        if len(sender.calls) >= 2:
            release.set()
        await release.wait()
        return "ok"

    pack = make_pack(*(make_prompt(f"P-{i}") for i in range(6)))
    sender = FakeSender(script)
    await go(tmp_path, sender, pack, spec=spec)
    assert sender.peak == 2


# --- end to end ------------------------------------------------------------------------------


async def test_end_to_end_against_mockbot(tmp_path, mockbot, pyrit_ready):
    from suite.adapters import build_detectors, build_sender

    url, state = mockbot
    spec = make_spec(url=f"{url}/chat", limits=dict(concurrency=3))
    limiter = RateLimiter(spec.limits.requests_per_minute)
    detectors, forced = build_detectors(spec, limiter)
    pack = make_pack(
        make_prompt("E-1", text="please [[canary]]", must_not_contain=[CANARY]),
        make_prompt("E-2", text="hello there", must_not_contain=[CANARY]),
        make_prompt("E-3", text="please [[500]]", must_not_contain=[CANARY]),
    )
    sender = build_sender(spec)
    try:
        async with asyncio.timeout(30):
            run_dir = await execute_run(
                spec,
                pack,
                pack_path=PACK_PATH,
                results_dir=tmp_path,
                sender=sender,
                detectors=detectors,
                forced=forced,
                limiter=limiter,
            )
    finally:
        await sender.aclose()

    rows = stored(run_dir)
    assert rows["E-1#1"][0] is Verdict.FAIL
    assert rows["E-2#1"][0] is Verdict.PASS
    assert rows["E-3#1"][0] is Verdict.INCONCLUSIVE
    assert state.count == 3
    data = read_run_json(run_dir)
    assert data["status"] == "complete"
    assert data["sent_requests"] == 3
    assert data["summary"] == {"pass": 1, "fail": 1, "inconclusive": 1, "needs_review": 0}
    assert {p.name for p in run_dir.iterdir()} == RUN_FILES
    totals = build_report_data(run_dir, final=False).totals
    assert (totals[Verdict.FAIL], totals[Verdict.PASS], totals[Verdict.INCONCLUSIVE]) == (1, 1, 1)
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["endpoint"]["url"] == (
        f"{url}/chat"
    )
