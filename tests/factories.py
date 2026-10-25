"""Test helpers shared across the suite's tests."""

from pathlib import Path
from typing import Any

from suite.config import EndpointSpec, masked
from suite.evidence import EvidenceStore, write_run_json
from suite.models import Attempt, DecidedBy, Judgement, SendError, SendOutcome, Verdict
from suite.prompts import Checks, Pack, Prompt


def make_spec(**overrides: Any) -> EndpointSpec:
    """Build a valid http EndpointSpec; keyword overrides replace the defaults.

    A ``limits=dict(...)`` override is merged into the default limits, so a test can set
    one limit without restating the others.
    """
    fields: dict[str, Any] = {
        "name": "t",
        "type": "http",
        "authorized": "test",
        "url": "http://127.0.0.1:9/chat",
        "body": {"message": "{prompt}"},
    }
    fields.update(overrides)
    return EndpointSpec(**fields)


def make_prompt(
    id: str = "P-1",
    category: str = "LLM01",
    text: str = "hello",
    repeat: int = 1,
    **checks: Any,
) -> Prompt:
    """Build a valid Prompt; extra keywords (``must_not_contain=[...]``, ...) become its checks."""
    return Prompt(id=id, category=category, prompt=text, repeat=repeat, checks=Checks(**checks))


def make_pack(*prompts: Prompt, name: str = "t") -> Pack:
    return Pack(name=name, prompts=list(prompts))


RUN_STARTED = "2026-10-23T14:05:09+00:00"
RUN_FINISHED = "2026-10-23T14:06:30+00:00"


def make_run_dir(
    root: Path,
    rows: list[tuple[str, str, Verdict, str | None]],
    *,
    redact: bool = True,
    repeat: dict[str, int] | None = None,
    hints: dict[str, str] | None = None,
) -> Path:
    """Build a run folder (``run.json`` and ``evidence.db``) under ``root`` and return it.

    Each row is ``(attempt_id, category, verdict, reply)``; the attempt id is
    ``"<prompt_id>#<repeat_index>"`` and a ``None`` reply means the bot sent nothing. Prompts
    appear in the order they are first mentioned. ``repeat`` maps a prompt id to its planned
    repeat count (default: the highest index seen), so a prompt can have attempts that were
    planned but never stored. ``hints`` maps an attempt id (or a prompt id, for all its
    attempts) to the judge's hint (``"refusal"`` / ``"no_refusal"``). The folder is built
    through the real ``EvidenceStore`` and ``write_run_json``, and is named like a run id,
    with ``-2``, ``-3`` and so on if ``root`` already holds one.
    """
    repeat = repeat or {}
    hints = hints or {}
    run_id = "20261023-140509-t"
    run_dir = root / run_id
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = root / f"{run_id}-{suffix}"
    run_dir.mkdir(parents=True)

    categories: dict[str, str] = {}
    highest: dict[str, int] = {}
    for attempt_id, category, _verdict, _reply in rows:
        prompt_id, _, index = attempt_id.rpartition("#")
        categories.setdefault(prompt_id, category)
        highest[prompt_id] = max(highest.get(prompt_id, 1), int(index))
    pack = Pack(
        name="t",
        prompts=[
            Prompt(
                id=prompt_id,
                category=category,
                prompt=f"Test prompt {prompt_id}",
                repeat=max(repeat.get(prompt_id, 1), highest[prompt_id]),
                pass_if="The bot answers safely",
            )
            for prompt_id, category in categories.items()
        ],
    )

    store = EvidenceStore(run_dir / "evidence.db")
    try:
        store.add_prompts(pack)
        counts = dict.fromkeys(Verdict, 0)
        for attempt_id, _category, verdict, reply in rows:
            prompt_id, _, index = attempt_id.rpartition("#")
            error = SendError("transport", "no reply") if reply is None else None
            outcome = SendOutcome(
                Attempt(attempt_id, prompt_id, int(index), f"Test prompt {prompt_id}"),
                reply,
                error,
                12,
                RUN_STARTED,
            )
            if error is not None:
                reasons: tuple[str, ...] = (str(error),)
            elif verdict is Verdict.FAIL:
                reasons = ("test: a check tripped",)
            else:
                reasons = ()
            decided = verdict in (Verdict.PASS, Verdict.FAIL)
            decided_by = DecidedBy.RULE if decided else DecidedBy.NONE
            hint = hints.get(attempt_id, hints.get(prompt_id, ""))
            store.add_attempt(outcome, Judgement(verdict, decided_by, reasons, hint))
            counts[verdict] += 1
    finally:
        store.close()

    write_run_json(
        run_dir,
        {
            "run_id": run_dir.name,
            "endpoint": masked(make_spec(headers={"Authorization": "Bearer s3cret-token"})),
            "pack": "t",
            "pack_path": "prompts/t.yaml",
            "started_at": RUN_STARTED,
            "finished_at": RUN_FINISHED,
            "status": "complete",
            "sent_requests": len(rows),
            "redact": redact,
            "summary": {verdict.value: count for verdict, count in counts.items()},
        },
    )
    return run_dir
