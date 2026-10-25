"""Run orchestration: engine -> judge -> evidence -> draft report, in one run folder.

``execute_run`` sends a pack to one endpoint and leaves ``results/<run-id>/`` behind: ``run.json``
(the run's facts, with the endpoint masked), ``evidence.db`` (every prompt and attempt as it
happened) and the draft reports. Each attempt is judged and stored the moment it finishes, so a
run that is aborted, cancelled or crashes still leaves usable evidence, and its ``run.json`` says
how it ended: ``complete``, ``aborted`` (too many consecutive errors) or ``interrupted``.
Attempts that were never stored show up in the reports as Inconclusive, never as a Pass.

The caller owns the sender (the CLI closes it) and does the printing; nothing is printed here.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from suite.config import EndpointSpec, masked
from suite.engine import RateLimiter, Sender, run_attempts
from suite.evidence import EvidenceStore, new_run_id, utc_now, write_run_json
from suite.judge import Detector, judge_outcome
from suite.models import SendOutcome, Verdict
from suite.planner import build_attempts
from suite.prompts import Pack
from suite.report import EVIDENCE_DB, write_reports

logger = logging.getLogger(__name__)


class _CountingSender:
    """Counts the requests handed to the real sender.

    ``RunStats`` carries the engine's own count, but only a run that finishes returns it; an
    interrupted run still has to say how many requests it sent. Same meaning as the engine's
    count: requests actually sent, retries included.
    """

    def __init__(self, inner: Sender) -> None:
        self._inner = inner
        self.sent = 0

    async def send(self, text: str) -> str:
        self.sent += 1
        return await self._inner.send(text)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _create_run_folder(results_dir: Path, run_id: str) -> Path:
    """Create ``results_dir/run_id``, or ``run_id-2``, ``-3``... if that name is taken.

    ``mkdir`` is the reservation, so two runs that start in the same second cannot share a folder.
    """
    results_dir.mkdir(parents=True, exist_ok=True)
    suffix = 1
    while True:
        folder = results_dir / (run_id if suffix == 1 else f"{run_id}-{suffix}")
        try:
            folder.mkdir()
        except FileExistsError:
            suffix += 1
            continue
        return folder


def _write_final_files(run_dir: Path, record: dict[str, Any], store: EvidenceStore) -> None:
    store.close()  # every attempt is already committed; the reports read the file afresh
    write_run_json(run_dir, record)
    write_reports(run_dir, final=False)


async def execute_run(
    spec: EndpointSpec,
    pack: Pack,
    *,
    pack_path: Path,
    results_dir: Path,
    sender: Sender,
    detectors: Mapping[str, Detector],
    forced: Sequence[str],
    limiter: RateLimiter,
    now: Callable[[], datetime] = utc_now,
) -> Path:
    """Run ``pack`` against ``spec`` and return the run folder.

    ``forced`` names detectors applied to every reply, whatever the prompt's own checks say.
    Cancellation (Ctrl+C) is handled like a normal end: ``run.json`` is written with status
    ``interrupted``, the draft reports are written, and then the ``CancelledError`` is re-raised.
    The same goes for any other exception, with that exception re-raised and, as the original
    failure matters most, a problem while writing the leftovers only logged.
    """
    started = now()
    run_dir = _create_run_folder(results_dir, new_run_id(spec.name, started))
    prompts = {prompt.id: prompt for prompt in pack.prompts}
    counter = _CountingSender(sender)
    # Shared with ``record`` below, so every run.json write sees the counts so far.
    summary = dict.fromkeys((verdict.value for verdict in Verdict), 0)
    record: dict[str, Any] = {
        "run_id": run_dir.name,
        # masked() needs this very spec object: the secrets it hides do not survive a copy.
        "endpoint": masked(spec),
        "pack": pack.name,
        "pack_path": str(pack_path),
        "started_at": started.isoformat(),
        "finished_at": None,
        "status": "running",
        "sent_requests": 0,
        "redact": spec.redact,
        "summary": summary,
    }

    store = EvidenceStore(run_dir / EVIDENCE_DB)

    async def on_outcome(outcome: SendOutcome) -> None:
        judgement = await judge_outcome(
            outcome, prompts[outcome.attempt.prompt_id], detectors=detectors, forced=forced
        )
        store.add_attempt(outcome, judgement)
        summary[judgement.verdict.value] += 1  # counts what was stored

    status = "interrupted"  # until the engine returns: cancelled, or any failure on the way
    try:
        write_run_json(run_dir, record)
        store.add_prompts(pack)
        stats = await run_attempts(
            build_attempts(pack),
            counter,
            spec.limits,
            on_outcome=on_outcome,
            limiter=limiter,
        )
        status = "aborted" if stats.aborted else "complete"
    finally:
        record.update(status=status, finished_at=now().isoformat(), sent_requests=counter.sent)
        if status == "interrupted":
            try:
                _write_final_files(run_dir, record, store)
            except Exception:
                logger.warning("could not write the final files of %s", run_dir, exc_info=True)
        else:
            _write_final_files(run_dir, record, store)
    return run_dir
