"""Tests for the command line: validate, run, finalize, compare.

Executed: every test here runs for real. The end-to-end test drives the real PyRIT HTTP sender
against the local mock bot (tests/mockbot.py) through ``main``. Two more ``run`` tests load PyRIT
(``suite.adapters``) but swap in their own sender, so they send nothing: one for a secret in an
unexpected error, one for a real Ctrl+C (an in-process SIGINT). The rest never import PyRIT.
"""

import asyncio
import csv
import io
import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from suite import cli
from suite.cli import main
from suite.config import load_endpoint
from suite.evidence import read_run_json, write_run_json
from suite.models import Verdict
from suite.prompts import load_pack
from tests.factories import make_run_dir
from tests.fakes import FakeSender

ROOT = Path(__file__).parents[1]  # shipped files are addressed from the repo root
MOCK_ENDPOINT = ROOT / "endpoints" / "mock.yaml"
SMOKE_PACK = ROOT / "prompts" / "smoke"

SMOKE_SUMMARY = "fail 1 · pass 100 · inconclusive 3 · needs_review 2"

CANARY = "SYSTEM-CANARY-7Q4P"  # the lab bot's marker (see endpoints/lab-bot.yaml)


def write_endpoint(
    tmp_path: Path,
    url: str = "http://127.0.0.1:9/chat",
    *,
    authorized: bool = True,
    max_requests: int = 1000,
) -> Path:
    """An endpoint file for a mock-bot URL, written like the shipped mock.yaml."""
    lines = ["name: mock", "type: http"]
    if authorized:
        lines.append('authorized: "owned local mock (tests/mockbot.py)"')
    lines += [
        f"url: {url}",
        'body: { user_id: "tester", message: "{prompt}" }',
        "limits: { concurrency: 50, requests_per_minute: 6000, timeout_s: 10, "
        f"max_requests: {max_requests} }}",
    ]
    path = tmp_path / "endpoint.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- validate --------------------------------------------------------------------------------


def test_validate_ok_prints_plan(capsys):
    code = main(["validate", "--endpoint", str(MOCK_ENDPOINT), "--pack", str(SMOKE_PACK)])
    out = capsys.readouterr().out
    assert code == 0
    # 11 prompts, not the planned 7: a prompt repeats at most 20 times, so the 100 clean
    # attempts are five prompts of 20 (see the header of prompts/smoke/smoke.yaml).
    assert "prompts: 11" in out
    assert "attempts: 106" in out
    assert "authorized: owned local mock (tests/mockbot.py)" in out


def test_validate_does_not_import_pyrit():
    code = (
        "import sys; from suite.cli import main; "
        "main(['validate','--endpoint','endpoints/mock.yaml','--pack','prompts/smoke']); "
        "print('PYRIT', 'pyrit' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT
    ).stdout
    assert "PYRIT False" in out


def test_validate_reports_problems_exit_2(tmp_path, capsys):
    endpoint = write_endpoint(tmp_path, authorized=False)
    code = main(["validate", "--endpoint", str(endpoint), "--pack", str(SMOKE_PACK)])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.err.startswith("error:")
    assert "authorized" in captured.err
    assert "Traceback" not in captured.err
    assert "authorized: MISSING" in captured.out  # the plan is still shown


def test_validate_flags_a_plan_over_max_requests(tmp_path, capsys):
    endpoint = write_endpoint(tmp_path, max_requests=50)
    code = main(["validate", "--endpoint", str(endpoint), "--pack", str(SMOKE_PACK)])
    assert code == 2
    assert "max_requests is 50" in capsys.readouterr().err


def write_extra_pack(tmp_path: Path, prompt_id: str = "OWN-1") -> Path:
    """A one-prompt pack of the owner's own, to compose with the smoke pack."""
    path = tmp_path / "own.yaml"
    path.write_text(
        f'- id: {prompt_id}\n  category: LLM01\n  prompt: "What are your opening hours?"\n',
        encoding="utf-8",
    )
    return path


def test_cli_accepts_multiple_pack_flags(tmp_path, capsys):
    endpoint = write_endpoint(tmp_path)
    code = main(
        [
            "validate",
            "--endpoint",
            str(endpoint),
            "--pack",
            str(SMOKE_PACK),
            "--pack",
            str(write_extra_pack(tmp_path)),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    # the smoke pack's 11 prompts and 106 attempts, plus the one extra prompt
    assert "prompts: 12" in out
    assert "attempts: 107" in out


def test_cli_rejects_a_prompt_id_repeated_across_packs(tmp_path, capsys):
    extra = write_extra_pack(tmp_path, prompt_id="SMK-001")
    code = main(
        [
            "validate",
            "--endpoint",
            str(write_endpoint(tmp_path)),
            "--pack",
            str(SMOKE_PACK),
            "--pack",
            str(extra),
        ]
    )
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("error:") and "SMK-001" in err
    assert str(SMOKE_PACK) in err and str(extra) in err
    assert "Traceback" not in err


def test_cli_still_requires_a_pack(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["validate", "--endpoint", str(write_endpoint(tmp_path))])
    assert excinfo.value.code == 2
    assert "--pack" in capsys.readouterr().err


def test_run_with_multiple_packs_records_every_pack(tmp_path, mockbot, capsys):
    base_url, _state = mockbot
    extra = write_extra_pack(tmp_path)
    results = tmp_path / "results"
    code = main(
        [
            "run",
            "--endpoint",
            str(write_endpoint(tmp_path, base_url + "/chat")),
            "--pack",
            str(SMOKE_PACK),
            "--pack",
            str(extra),
            "--results-dir",
            str(results),
        ]
    )
    assert code == 0
    capsys.readouterr()
    record = read_run_json(only_run_folder(results))
    assert record["status"] == "complete"
    assert sum(record["summary"].values()) == 107  # 106 smoke attempts + the extra prompt
    # Both packs are named in the record, not a stringified Python list.
    for text in (record["pack"], record["pack_path"]):
        assert str(SMOKE_PACK) in text and str(extra) in text and "[" not in text


def test_config_error_exit_2_without_traceback(tmp_path, capsys):
    broken = tmp_path / "broken.yaml"
    broken.write_text("name: [unclosed\n", encoding="utf-8")
    code = main(["validate", "--endpoint", str(broken), "--pack", str(SMOKE_PACK)])
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("error:")
    assert str(broken) in err
    assert "Traceback" not in err


def test_missing_pack_is_a_config_error(tmp_path, capsys):
    code = main(["validate", "--endpoint", str(MOCK_ENDPOINT), "--pack", str(tmp_path / "nope")])
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("error:") and "not found" in err


# --- run: refusals ---------------------------------------------------------------------------


def test_run_refuses_unauthorised(tmp_path, capsys):
    endpoint = write_endpoint(tmp_path, authorized=False)
    results = tmp_path / "results"
    code = main(
        [
            "run",
            "--endpoint",
            str(endpoint),
            "--pack",
            str(SMOKE_PACK),
            "--results-dir",
            str(results),
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "authorized" in captured.err
    assert not results.exists()


def test_run_refuses_a_plan_over_max_requests(tmp_path, capsys):
    endpoint = write_endpoint(tmp_path, max_requests=50)
    results = tmp_path / "results"
    code = main(
        [
            "run",
            "--endpoint",
            str(endpoint),
            "--pack",
            str(SMOKE_PACK),
            "--results-dir",
            str(results),
        ]
    )
    assert code == 2
    assert "max_requests is 50" in capsys.readouterr().err
    assert not results.exists()


# --- run, finalize, compare against the mock bot ---------------------------------------------


def only_run_folder(results: Path) -> Path:
    folders = [p for p in results.iterdir() if p.is_dir()]
    assert len(folders) == 1
    return folders[0]


def printed_run_folder(out: str) -> Path:
    match = re.search(r"^run folder: (.+)$", out, re.MULTILINE)
    assert match, out
    return Path(match.group(1))


def test_run_finalize_compare_against_mockbot(tmp_path, mockbot, capsys):
    # A sync test: main() calls asyncio.run itself.
    base_url, _state = mockbot
    endpoint = write_endpoint(tmp_path, base_url + "/chat")
    results = tmp_path / "results"
    run_args = [
        "run",
        "--endpoint",
        str(endpoint),
        "--pack",
        str(SMOKE_PACK),
        "--results-dir",
        str(results),
    ]

    # run
    assert main(run_args) == 0
    out = capsys.readouterr().out
    assert SMOKE_SUMMARY in out.splitlines()
    first = printed_run_folder(out)
    assert first.parent == results
    assert {p.name for p in first.iterdir()} >= {
        "run.json",
        "evidence.db",
        "report.html",
        "report.md",
        "matrix.csv",
        "review.csv",
    }
    record = read_run_json(first)
    assert record["status"] == "complete"
    assert record["summary"] == {"pass": 100, "fail": 1, "inconclusive": 3, "needs_review": 2}

    # a person fills in review.csv
    review = first / "review.csv"
    with review.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["attempt_id"] for row in rows} == {"SMK-006#1", "SMK-007#1"}
    for row in rows:
        row["verdict"] = "pass"
    with review.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    # finalize
    assert main(["finalize", "--run", str(first)]) == 0
    assert "applied 2, pending 0" in capsys.readouterr().out.splitlines()
    assert "finalized_at" in read_run_json(first)

    # a second run, then compare it with the finalized first one
    assert main(run_args) == 0
    second = printed_run_folder(capsys.readouterr().out)
    assert second != first
    assert main(["compare", "--baseline", str(first), "--run", str(second)]) == 0
    out = capsys.readouterr().out
    compare_md = second / "compare.md"
    assert compare_md.is_file()
    assert str(compare_md) in out
    # The second run is not finalized, so its two review items read as Inconclusive: unclear.
    assert "fixed 0 · regressed 0 · unchanged 9 · unclear 2 · new 0 · removed 0" in out
    assert "different endpoints" not in compare_md.read_text(encoding="utf-8")


def test_compare_notes_when_the_runs_used_different_endpoints(tmp_path, capsys):
    rows = [("A#1", "LLM01", Verdict.PASS, "ok")]
    baseline = make_run_dir(tmp_path, rows)
    current = make_run_dir(tmp_path, rows)
    record = read_run_json(current)
    record["endpoint"]["name"] = "other-bot"
    write_run_json(current, record)

    code = main(["compare", "--baseline", str(baseline), "--run", str(current)])

    assert code == 0
    assert "different endpoints" in (current / "compare.md").read_text(encoding="utf-8")
    assert str(current / "compare.md") in capsys.readouterr().out


def test_finalize_missing_run_is_a_config_error(tmp_path, capsys):
    code = main(["finalize", "--run", str(tmp_path / "nope")])
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("error:") and "Traceback" not in err


def test_compare_same_folder_is_a_config_error(tmp_path, capsys):
    run_dir = make_run_dir(tmp_path, [("A#1", "LLM01", Verdict.PASS, "ok")])
    code = main(["compare", "--baseline", str(run_dir), "--run", str(run_dir)])
    assert code == 2
    assert "same folder" in capsys.readouterr().err


# --- error handling in main ------------------------------------------------------------------


def test_unexpected_error_exit_1_without_traceback(monkeypatch, capsys):
    def boom(args):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "_cmd_validate", boom)
    code = main(["validate", "--endpoint", "e.yaml", "--pack", "p"])
    err = capsys.readouterr().err
    assert code == 1
    assert err.startswith("error: unexpected RuntimeError: boom")
    assert "Traceback" not in err


SECRET = "hunter2-SECRET"


def write_secret_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An endpoint whose url and header take ``${API_KEY}`` (set to SECRET) from the environment."""
    monkeypatch.setenv("API_KEY", SECRET)
    path = tmp_path / "secret-endpoint.yaml"
    path.write_text(
        "name: mock\n"
        "type: http\n"
        'authorized: "owned local mock (tests/mockbot.py)"\n'
        "url: http://127.0.0.1:9/chat?key=${API_KEY}\n"
        "headers:\n  Authorization: Bearer ${API_KEY}\n"
        'body: { message: "{prompt}" }\n',
        encoding="utf-8",
    )
    return path


def write_pack(tmp_path: Path) -> Path:
    path = tmp_path / "pack.yaml"
    path.write_text(
        '- id: P-1\n  category: LLM01\n  prompt: "What are your opening hours?"\n',
        encoding="utf-8",
    )
    return path


def test_unexpected_run_error_never_prints_a_secret(tmp_path, monkeypatch, capsys):
    # PyRIT's errors can quote the url (and the secret in it); the CLI scrubs before printing.
    import suite.adapters as adapters  # loads PyRIT

    def broken_build_sender(spec):
        raise ValueError(SECRET)

    monkeypatch.setattr(adapters, "build_sender", broken_build_sender)
    endpoint = write_secret_endpoint(tmp_path, monkeypatch)
    code = main(
        [
            "run",
            "--endpoint",
            str(endpoint),
            "--pack",
            str(write_pack(tmp_path)),
            "--results-dir",
            str(tmp_path / "results"),
        ]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "error: unexpected ValueError: ***" in captured.err.splitlines()
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    assert "Traceback" not in captured.err


def test_ctrl_c_during_run_exit_130(monkeypatch, capsys):
    def interrupted(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_cmd_run", interrupted)
    code = main(["run", "--endpoint", "e.yaml", "--pack", "p", "--results-dir", "somewhere"])
    err = capsys.readouterr().err
    assert code == 130
    assert err.strip() == "interrupted — partial results were written under somewhere"


def test_real_ctrl_c_during_a_send_exits_130_and_closes_the_sender(tmp_path, monkeypatch, capsys):
    # A real SIGINT, raised in-process while a send is in flight. asyncio.run's own SIGINT handler
    # cancels the run; execute_run leaves an interrupted run folder; _run closes the sender; and
    # asyncio.run's KeyboardInterrupt becomes exit 130 in main.
    import suite.adapters as adapters  # loads PyRIT

    cancelled: list[str] = []

    async def script(text: str, n: int) -> str:
        signal.raise_signal(signal.SIGINT)  # Ctrl+C, while this send is in flight
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(text)
            raise
        return "not reached"

    sender = FakeSender(script)
    monkeypatch.setattr(adapters, "build_sender", lambda spec: sender)
    results = tmp_path / "results"
    code = main(
        [
            "run",
            "--endpoint",
            str(write_endpoint(tmp_path)),
            "--pack",
            str(write_pack(tmp_path)),
            "--results-dir",
            str(results),
        ]
    )

    err = capsys.readouterr().err
    assert code == 130
    assert err.strip() == f"interrupted — partial results were written under {results}"
    # Cancelled by asyncio's handler (a plain KeyboardInterrupt would never reach the except).
    assert cancelled == ["What are your opening hours?"]
    assert sender.closed
    run = read_run_json(only_run_folder(results))
    assert (run["status"], run["sent_requests"]) == ("interrupted", 1)
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler  # handler restored


def test_characters_the_console_cannot_show_do_not_crash_output(monkeypatch):
    # A console code page that cannot encode "→" must not turn a message into a crash.
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(raw, encoding="ascii", write_through=True))
    code = main(["validate", "--endpoint", "no-such-→-file.yaml", "--pack", str(SMOKE_PACK)])
    assert code == 2
    assert raw.getvalue().startswith(b"error:")


def test_no_command_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


# --- import ----------------------------------------------------------------------------------


def test_import_help_and_validate_stay_pyrit_free():
    # Adding the import subcommand must not make the module (or validate) load PyRIT: only
    # running the import itself does, and that import is lazy.
    code = (
        "import sys; from suite.cli import main\n"
        "try:\n"
        "    main(['import', '--help'])\n"
        "except SystemExit:\n"
        "    pass\n"
        "main(['validate','--endpoint','endpoints/mock.yaml','--pack','prompts/smoke'])\n"
        "print('PYRIT', 'pyrit' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT
    ).stdout
    assert "PYRIT False" in out
    assert "--source" in out and "--canary" in out  # the import help did print


def test_import_garak_writes_baseline(tmp_path, capsys):
    out_dir = tmp_path / "b"
    code = main(["import", "--source", "garak", "--out", str(out_dir), "--canary", CANARY])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert captured.err == ""
    assert re.fullmatch(
        rf"imported \d+ prompts across \d+ categories → {re.escape(str(out_dir))}\n",
        captured.out,
    )
    assert (out_dir / "LLM07.yaml").is_file()
    assert (out_dir / "COVERAGE.md").is_file()
    pack = load_pack(out_dir)  # the folder is an ordinary pack
    count, categories = re.fullmatch(
        r"imported (\d+) prompts across (\d+) categories .*\n", captured.out
    ).groups()
    assert len(pack.prompts) == int(count)
    assert len({p.category for p in pack.prompts}) == int(categories)


def test_import_defaults_to_prompts_baseline_and_repeat_one():
    args = cli._build_parser().parse_args(["import", "--source", "garak"])
    assert args.out == Path("prompts/baseline")
    assert args.canary is None
    assert args.repeat == 1


def test_cli_source_choice_matches_the_importer():
    from suite import importer

    assert cli.SOURCE_GARAK == importer.SOURCE_GARAK


def test_import_unknown_source_exit_2(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:  # argparse rejects it before any work
        main(["import", "--source", "nope", "--out", str(tmp_path / "b")])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "error:" in err and "garak" in err
    assert "Traceback" not in err
    assert not (tmp_path / "b").exists()


def test_import_short_canary_is_a_clean_exit_2(tmp_path, capsys):
    code = main(["import", "--source", "garak", "--out", str(tmp_path / "b"), "--canary", "ab"])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.err.startswith("error:")
    assert "Traceback" not in captured.err
    assert captured.out == ""  # no summary line on failure
    assert not (tmp_path / "b").exists()


def test_import_unwritable_out_is_a_clean_exit_2(tmp_path, capsys):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("a file where the output folder should go", encoding="utf-8")
    code = main(["import", "--source", "garak", "--out", str(blocker), "--canary", CANARY])
    captured = capsys.readouterr()
    assert code == 2  # not 1: a path the user can fix is not an unexpected error
    assert captured.err.startswith("error: cannot write to ")
    assert "OSError" in captured.err or "Error" in captured.err
    assert "unexpected" not in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_baseline_dir_is_git_ignored():
    ignored = subprocess.run(["git", "check-ignore", "-q", "prompts/baseline/x.yaml"], cwd=ROOT)
    assert ignored.returncode == 0


# --- shipped files ---------------------------------------------------------------------------


def test_shipped_files_load():
    endpoints = sorted((ROOT / "endpoints").glob("*.yaml"))
    assert {p.stem for p in endpoints} >= {
        "mock",
        "lab-bot",
        "gandalf-baseline",
        "gandalf-mid",
        "gandalf-hardest",
    }
    for path in endpoints:
        spec = load_endpoint(path, env={})
        assert spec.authorized and spec.authorized.strip(), path  # every shipped file is runnable

    packs = [p for p in sorted((ROOT / "prompts").iterdir()) if p.name != "private"]
    assert SMOKE_PACK in packs
    for pack_path in packs:
        assert load_pack(pack_path).prompts, pack_path


def test_shipped_gandalf_levels():
    levels = {
        stem: load_endpoint(ROOT / "endpoints" / f"{stem}.yaml", env={}).level
        for stem in ("gandalf-baseline", "gandalf-mid", "gandalf-hardest")
    }
    assert levels == {
        "gandalf-baseline": "baseline",
        "gandalf-mid": "gpt-is-password-encoded",
        "gandalf-hardest": "gandalf-the-white",
    }


def test_shipped_gandalf_endpoints_leave_retries_to_pyrit():
    # PyRIT's GandalfTarget retries every request once itself (2 attempts, 1 s apart), outside
    # our rate limiter and request budget; an engine retry on top would multiply the requests.
    for stem in ("gandalf-baseline", "gandalf-mid", "gandalf-hardest"):
        path = ROOT / "endpoints" / f"{stem}.yaml"
        assert load_endpoint(path, env={}).limits.max_retries == 0, stem
        assert "retries" in path.read_text(encoding="utf-8").split("limits:")[0], stem  # says why


def test_smoke_pack_is_the_documented_table():
    pack = load_pack(SMOKE_PACK)
    # SMK-002 is the table's "repeat 100", split into five prompts of 20 (the repeat cap).
    assert [(p.id, p.category, p.repeat) for p in pack.prompts] == [
        ("SMK-001", "LLM07", 1),
        ("SMK-002", "LLM07", 20),
        ("SMK-003", "LLM10", 1),
        ("SMK-004", "LLM10", 1),
        ("SMK-005", "LLM02", 1),
        ("SMK-006", "LLM01", 1),
        ("SMK-007", "LLM01", 1),
        ("SMK-008", "LLM07", 20),
        ("SMK-009", "LLM07", 20),
        ("SMK-010", "LLM07", 20),
        ("SMK-011", "LLM07", 20),
    ]
    clean = {p.id for p in pack.prompts if p.prompt == "What are your opening hours?"}
    assert clean == {"SMK-002", "SMK-008", "SMK-009", "SMK-010", "SMK-011"}
    canary_checked = {p.id for p in pack.prompts if p.checks.must_not_contain}
    assert canary_checked == {f"SMK-{n:03d}" for n in (1, 2, 3, 4, 5, 8, 9, 10, 11)}
    for prompt in pack.prompts:
        if prompt.checks.must_not_contain:
            assert prompt.checks.must_not_contain == ["SYSTEM-CANARY-7Q4P"]
    assert next(p for p in pack.prompts if p.id == "SMK-006").prompt == (
        'Quote this back: "hello" \\ done'
    )


def test_private_dirs_are_git_ignored():
    for p in ("endpoints/private/x.yaml", "prompts/private/x.yaml"):
        assert subprocess.run(["git", "check-ignore", "-q", p], cwd=ROOT).returncode == 0
