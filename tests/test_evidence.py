import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from suite.evidence import (
    AttemptRow,
    EvidenceStore,
    PromptRow,
    new_run_id,
    read_run_json,
    utc_now,
    write_run_json,
)
from suite.models import (
    Attempt,
    ConfigError,
    DecidedBy,
    Judgement,
    SendError,
    SendOutcome,
    Verdict,
)
from tests.factories import make_pack, make_prompt

STARTED = "2026-10-23T14:05:09+00:00"


def outcome(
    prompt_id: str = "P-1",
    index: int = 1,
    reply: str | None = "a reply",
    error: SendError | None = None,
    latency_ms: int = 12,
    started_at: str = STARTED,
) -> SendOutcome:
    attempt = Attempt(f"{prompt_id}#{index}", prompt_id, index, "sent text")
    return SendOutcome(attempt, reply, error, latency_ms, started_at)


def judgement(
    verdict: Verdict = Verdict.PASS,
    decided_by: DecidedBy = DecidedBy.RULE,
    reasons: tuple[str, ...] = (),
    hint: str = "refusal",
) -> Judgement:
    return Judgement(verdict, decided_by, reasons, hint)


def two_prompt_pack():
    return make_pack(
        make_prompt("P-1", "LLM01", "Say 日本語 and 'quotes'; DROP TABLE prompts;--", repeat=2),
        make_prompt("P-2", "LLM07", "line one\nline two", must_not_contain=["SYSTEM-CANARY-7Q4P"]),
    )


def test_roundtrip_prompts_and_attempts(tmp_path: Path) -> None:
    pack = two_prompt_pack()
    pack.prompts[1] = pack.prompts[1].model_copy(update={"pass_if": "no canary"})
    store = EvidenceStore(tmp_path / "evidence.db")
    store.add_prompts(pack)
    store.add_attempt(
        outcome("P-1", 1, reply="Fine. Contact alice@example.com — ünïcode ✓"),
        judgement(Verdict.PASS, DecidedBy.RULE, (), "refusal"),
    )
    store.add_attempt(
        outcome("P-1", 2, reply="The code is SYSTEM-CANARY-7Q4P", latency_ms=340),
        judgement(
            Verdict.FAIL,
            DecidedBy.RULE,
            ("contains 'SYSTEM-CANARY-7Q4P' (plain text)", "matches email"),
            "no_refusal",
        ),
    )
    store.add_attempt(
        outcome(
            "P-2",
            1,
            reply=None,
            error=SendError("http_status", "server said 500", status=500),
            latency_ms=7,
            started_at="2026-10-23T14:05:10+00:00",
        ),
        judgement(Verdict.INCONCLUSIVE, DecidedBy.NONE, ("http_status: server said 500",), ""),
    )

    assert store.prompts() == [
        PromptRow(
            "P-1",
            "LLM01",
            "Say 日本語 and 'quotes'; DROP TABLE prompts;--",
            "",
            {"must_not_contain": [], "must_not_match": [], "detectors": []},
            2,
            1,
        ),
        PromptRow(
            "P-2",
            "LLM07",
            "line one\nline two",
            "no canary",
            {"must_not_contain": ["SYSTEM-CANARY-7Q4P"], "must_not_match": [], "detectors": []},
            1,
            2,
        ),
    ]
    assert store.attempts() == [
        AttemptRow(
            "P-1#1",
            "P-1",
            1,
            "Fine. Contact alice@example.com — ünïcode ✓",
            Verdict.PASS,
            DecidedBy.RULE,
            (),
            "refusal",
            None,
            None,
            12,
            STARTED,
            "",
        ),
        AttemptRow(
            "P-1#2",
            "P-1",
            2,
            "The code is SYSTEM-CANARY-7Q4P",
            Verdict.FAIL,
            DecidedBy.RULE,
            ("contains 'SYSTEM-CANARY-7Q4P' (plain text)", "matches email"),
            "no_refusal",
            None,
            None,
            340,
            STARTED,
            "",
        ),
        AttemptRow(
            "P-2#1",
            "P-2",
            1,
            None,
            Verdict.INCONCLUSIVE,
            DecidedBy.NONE,
            ("http_status: server said 500",),
            "",
            "http_status",
            500,
            7,
            "2026-10-23T14:05:10+00:00",
            "",
        ),
    ]
    store.close()


def test_stored_values_are_enum_strings_and_json(tmp_path: Path) -> None:
    path = tmp_path / "evidence.db"
    store = EvidenceStore(path)
    store.add_prompts(two_prompt_pack())
    store.add_attempt(
        outcome("P-1", 1), judgement(Verdict.FAIL, DecidedBy.RULE, ("because", "日本語"), "")
    )
    store.close()

    with closing(sqlite3.connect(path)) as raw:
        verdict, decided_by, reasons = raw.execute(
            "SELECT verdict, decided_by, reasons_json FROM attempts"
        ).fetchone()
        (checks,) = raw.execute("SELECT checks_json FROM prompts WHERE prompt_id='P-2'").fetchone()
    assert (verdict, decided_by) == ("fail", "rule")
    assert json.loads(reasons) == ["because", "日本語"]
    assert json.loads(checks)["must_not_contain"] == ["SYSTEM-CANARY-7Q4P"]


def test_reopen_persists(tmp_path: Path) -> None:
    path = tmp_path / "evidence.db"
    store = EvidenceStore(path)
    store.add_prompts(two_prompt_pack())
    store.add_attempt(outcome("P-1", 1), judgement())
    prompts, attempts = store.prompts(), store.attempts()
    store.close()

    reopened = EvidenceStore(path)  # an existing file keeps its data
    assert reopened.prompts() == prompts
    assert reopened.attempts() == attempts
    reopened.close()


def test_close_twice_is_safe(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.db")
    store.close()
    store.close()


def test_each_attempt_is_committed_when_added(tmp_path: Path) -> None:
    # A second connection sees the row while the first is still open and has not closed:
    # evidence survives a crash or Ctrl+C part-way through a run.
    path = tmp_path / "evidence.db"
    store = EvidenceStore(path)
    store.add_prompts(two_prompt_pack())
    store.add_attempt(outcome("P-1", 1), judgement())

    with closing(sqlite3.connect(path)) as other:
        assert other.execute("SELECT COUNT(*) FROM attempts").fetchone() == (1,)
        assert other.execute("SELECT COUNT(*) FROM prompts").fetchone() == (2,)
    store.close()


def test_rows_are_ordered_by_pack_position_then_repeat(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.db")
    store.add_prompts(
        make_pack(make_prompt("Z-last-alpha", repeat=2), make_prompt("A-first-alpha"))
    )
    # Finished out of order, as a parallel run would.
    for prompt_id, index in [("A-first-alpha", 1), ("Z-last-alpha", 2), ("Z-last-alpha", 1)]:
        store.add_attempt(outcome(prompt_id, index), judgement())

    assert [p.prompt_id for p in store.prompts()] == ["Z-last-alpha", "A-first-alpha"]
    assert [p.position for p in store.prompts()] == [1, 2]
    assert [a.attempt_id for a in store.attempts()] == [
        "Z-last-alpha#1",
        "Z-last-alpha#2",
        "A-first-alpha#1",
    ]
    store.close()


def test_attempt_for_unknown_prompt_is_refused(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.db")
    store.add_prompts(two_prompt_pack())
    with pytest.raises(sqlite3.IntegrityError):
        store.add_attempt(outcome("NOPE", 1), judgement())
    assert store.attempts() == []
    store.close()


def test_reply_with_a_lone_surrogate_is_kept_as_visible_text(tmp_path: Path) -> None:
    # json.loads('"\\ud83d"') yields a lone surrogate, which sqlite3 cannot encode as UTF-8.
    # A buggy bot must not make the evidence sink raise.
    store = EvidenceStore(tmp_path / "evidence.db")
    store.add_prompts(two_prompt_pack())
    store.add_attempt(
        outcome("P-1", 1, reply="before \ud83d after"),
        judgement(Verdict.FAIL, DecidedBy.RULE, ("note \ud83d",), ""),
    )
    (row,) = store.attempts()
    assert row.reply_text == "before \\ud83d after"
    assert row.reasons == ("note \\ud83d",)
    store.close()


def test_set_human_verdict_only_for_queue(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.db")
    store.add_prompts(two_prompt_pack())
    store.add_attempt(
        outcome("P-1", 1), judgement(Verdict.NEEDS_REVIEW, DecidedBy.NONE, (), "no_refusal")
    )
    store.add_attempt(outcome("P-1", 2), judgement(Verdict.PASS, DecidedBy.RULE, (), "refusal"))
    store.add_attempt(
        outcome("P-2", 1, reply=None, error=SendError("timeout", "slow")),
        judgement(Verdict.INCONCLUSIVE, DecidedBy.NONE, ("timeout: slow",), ""),
    )

    store.set_human_verdict("P-1#1", Verdict.PASS, "looks safe — 日本語 ok")
    row = store.attempts()[0]
    assert (row.verdict, row.decided_by, row.human_note) == (
        Verdict.PASS,
        DecidedBy.HUMAN,
        "looks safe — 日本語 ok",
    )
    assert (row.reply_text, row.hint, row.reasons) == ("a reply", "no_refusal", ())

    # A person may change their own earlier decision.
    store.set_human_verdict("P-1#1", Verdict.FAIL, "second look: it leaks")
    row = store.attempts()[0]
    assert (row.verdict, row.decided_by, row.human_note) == (
        Verdict.FAIL,
        DecidedBy.HUMAN,
        "second look: it leaks",
    )

    # A rule decided the other two; a person cannot overwrite them.
    before = store.attempts()
    for attempt_id in ("P-1#2", "P-2#1"):
        with pytest.raises(ConfigError) as caught:
            store.set_human_verdict(attempt_id, Verdict.FAIL, "no")
        assert str(caught.value) == (
            f"attempt {attempt_id} was decided by rule; only queued items can be reviewed"
        )
    assert store.attempts() == before
    store.close()


def test_set_human_verdict_rejects_unknown_attempt_and_non_decisions(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.db")
    store.add_prompts(two_prompt_pack())
    store.add_attempt(outcome("P-1", 1), judgement(Verdict.NEEDS_REVIEW, DecidedBy.NONE))

    with pytest.raises(ConfigError, match="unknown attempt P-9#1"):
        store.set_human_verdict("P-9#1", Verdict.PASS, "")
    with pytest.raises(ConfigError, match="needs_review"):
        store.set_human_verdict("P-1#1", Verdict.NEEDS_REVIEW, "")
    assert store.attempts()[0].decided_by is DecidedBy.NONE
    store.close()


def test_opening_a_file_that_is_not_a_database_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "evidence.db"
    path.write_bytes(b"this is not a sqlite database, just some text" * 50)
    with pytest.raises(ConfigError, match="evidence.db"):
        EvidenceStore(path)


def test_opening_in_a_missing_folder_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="evidence.db"):
        EvidenceStore(tmp_path / "no-such-run" / "evidence.db")


def test_new_run_id() -> None:
    now = datetime(2026, 10, 23, 14, 5, 9, tzinfo=UTC)
    assert new_run_id("lab-bot", now) == "20261023-140509-lab-bot"


def test_utc_now_is_timezone_aware_utc() -> None:
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() is not None and now.utcoffset().total_seconds() == 0


def test_run_json_roundtrip_utf8(tmp_path: Path) -> None:
    data = {"pack": "日本語", "nested": {"n": 3, "items": ["a", "é"]}}
    write_run_json(tmp_path, data)

    assert read_run_json(tmp_path) == data
    text = (tmp_path / "run.json").read_bytes().decode("utf-8")
    assert "日本語" in text  # written as characters, not \u escapes
    assert '\n  "pack"' in text  # indent=2


def test_write_run_json_replaces_atomically(tmp_path: Path) -> None:
    write_run_json(tmp_path, {"version": 1})
    write_run_json(tmp_path, {"version": 2})
    assert read_run_json(tmp_path) == {"version": 2}

    # A failure while writing leaves the previous file untouched and no temp file behind.
    with pytest.raises(TypeError):
        write_run_json(tmp_path, {"version": 3, "bad": object()})
    assert read_run_json(tmp_path) == {"version": 2}
    assert [p.name for p in tmp_path.iterdir()] == ["run.json"]


def test_read_run_json_problems_are_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="run.json"):
        read_run_json(tmp_path)  # no file

    (tmp_path / "run.json").write_text('{"cut": ', encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        read_run_json(tmp_path)

    (tmp_path / "run.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ConfigError, match="JSON object"):
        read_run_json(tmp_path)
