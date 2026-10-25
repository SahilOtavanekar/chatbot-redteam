"""Reports for a run folder: ``report.html``, ``report.md``, ``matrix.csv`` and ``review.csv``.

Everything is rendered from the run's own files (``run.json`` and ``evidence.db``), so a report
can be rebuilt at any time, in particular after ``finalize`` has merged human verdicts.

Three rules shape the output:

* **Inconclusive is never a pass.** An attempt that was planned but never stored counts as
  Inconclusive (``not_run: never sent``), and in a *final* report any attempt still waiting for
  review also becomes Inconclusive (``not reviewed``).
* **Replies are redacted here, not in the database.** ``evidence.db`` keeps raw replies; when
  ``run.json`` says ``redact`` (the default) every reply, reason and note goes through
  :func:`suite.patterns.redact` on its way into a report. Prompt text is never redacted.
* **Untrusted text stays inert.** A bot's reply is data, not markup. HTML relies on Jinja
  autoescape (nothing is marked safe), Markdown cells go through :func:`md_cell`, and CSV cells
  through :func:`csv_cell` so a spreadsheet never runs a reply as a formula.
"""

import csv
import re
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined

from suite.evidence import EvidenceStore, read_run_json
from suite.models import ConfigError, DecidedBy, Verdict, worst
from suite.patterns import redact
from suite.prompts import OWASP_LLM_2025

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

EVIDENCE_DB = "evidence.db"
NOT_RUN_REASON = "not_run: never sent"
NOT_REVIEWED_REASON = "not reviewed"
EXCERPT_CHARS = 200

MATRIX_COLUMNS = (
    "attempt_id",
    "prompt_id",
    "category",
    "verdict",
    "decided_by",
    "reasons",
    "hint",
    "error_kind",
    "http_status",
    "latency_ms",
    "reply_excerpt",
)
REVIEW_COLUMNS = (
    "attempt_id",
    "prompt_id",
    "category",
    "hint",
    "pass_if",
    "prompt",
    "reply",
    "verdict",
    "note",
)

# Shown near the top of report.html and report.md. A rule Pass means the configured checks held,
# nothing more, so the reports say so.
VERDICT_LEGEND = (
    "Pass (rule) = every configured check held — it is not proof the bot is safe. "
    "Pass (human) = a reviewer judged the reply against pass_if. "
    "Fail = a check tripped or a reviewer marked it failed. "
    "Inconclusive = no usable reply, or a check could not run — never counted as a pass. "
    "Needs review = no automatic check; see review.csv."
)

_VERDICT_WORDS = {
    Verdict.PASS: "Pass",
    Verdict.FAIL: "Fail",
    Verdict.INCONCLUSIVE: "Inconclusive",
    Verdict.NEEDS_REVIEW: "Needs review",
}


def csv_cell(text: str) -> str:
    """Neutralise a CSV text cell: one that starts like a spreadsheet formula gets a ``'``."""
    return "'" + text if text.startswith(FORMULA_PREFIXES) else text


# Markdown syntax a reply could use to fetch or link something: backslash-escaped, so a previewer
# shows the characters instead of acting on them. ``<`` and ``>`` become entities (no tags).
# Bare URLs are defanged (``https[:]//``, ``www[.]``) because previewers autolink them, and an
# autolink to an attacker's address is a click (or, for an image, a silent fetch) away.
_MD_ENTITIES = {"<": "&lt;", ">": "&gt;"}
_MD_TOKEN = re.compile(r"(https?)://|www\.|[\\`*_\[\]()!#~|<>]", re.IGNORECASE)


def _md_unit(match: re.Match[str]) -> str:
    """The inert text for one Markdown-significant token."""
    token = match.group()
    if match.group(1):  # http:// or https://
        return f"{match.group(1)}[:]//"
    if token.lower() == "www.":
        return f"{token[:3]}[.]"
    return _MD_ENTITIES.get(token) or "\\" + token


def _md_units(text: str) -> Iterator[str]:
    """``text`` as inert units: each plain character, and each token's escape as one whole unit."""
    position = 0
    for match in _MD_TOKEN.finditer(text):
        yield from text[position : match.start()]
        yield _md_unit(match)
        position = match.end()
    yield from text[position:]


def md_cell(text: str, limit: int = 200) -> str:
    r"""Make untrusted ``text`` inert inside one Markdown table cell, at most ``limit`` long.

    Newlines become spaces. ``|``, ``\``, backtick, ``*``, ``_``, ``[``, ``]``, ``(``, ``)``,
    ``!``, ``#`` and ``~`` are backslash-escaped, ``<`` and ``>`` become entities, and
    ``http://``, ``https://`` and ``www.`` are defanged (``http[:]//``, ``www[.]``), so a reply
    cannot render as a link, an image (which a previewer would fetch), a code span, a tag or an
    autolink. If the result is longer than ``limit`` (escapes count) it is cut and ``…`` is
    added; the cut falls between units, never inside an escape such as ``\*`` or ``&lt;``.
    """
    flat = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    pieces: list[str] = []
    length = 0
    for piece in _md_units(flat):
        if length + len(piece) > limit:
            return "".join(pieces) + "…"
        pieces.append(piece)
        length += len(piece)
    return "".join(pieces)


@dataclass
class AttemptView:
    """One attempt as a report shows it (redacted, and with the final-report rules applied)."""

    attempt_id: str
    verdict: Verdict
    decided_by: DecidedBy
    reasons: tuple[str, ...]
    hint: str
    reply: str | None
    error_kind: str | None
    http_status: int | None
    latency_ms: int | None
    human_note: str


@dataclass
class PromptView:
    """A prompt with its attempts; ``verdict`` is the worst of them."""

    prompt_id: str
    category: str
    verdict: Verdict
    pass_if: str
    prompt_text: str
    attempts: list[AttemptView]


@dataclass
class CategoryRow:
    """One OWASP category's prompt counts. ``pass_rate`` is a percentage of ``total``."""

    code: str
    name: str
    total: int
    passed: int
    failed: int
    inconclusive: int
    needs_review: int
    pass_rate: float


@dataclass
class ReportData:
    """Everything a report shows. ``totals`` and ``categories`` count prompts, not attempts;
    ``pending`` counts attempts still waiting for a human (always 0 in a final report)."""

    run: dict[str, Any]
    final: bool
    totals: dict[Verdict, int]
    categories: list[CategoryRow]
    prompts: list[PromptView]
    pending: int


def build_report_data(run_dir: Path, *, final: bool) -> ReportData:
    """Read a run folder into :class:`ReportData`.

    With ``final`` set, attempts still queued for review become Inconclusive (``not reviewed``).
    Raises ConfigError if ``run.json`` or ``evidence.db`` is missing.
    """
    run = read_run_json(run_dir)
    db_path = run_dir / EVIDENCE_DB
    # Checked first: EvidenceStore would create an empty database and report an empty run.
    if not db_path.is_file():
        raise ConfigError(f"{run_dir}: no {EVIDENCE_DB} found; is this a run folder?")
    store = EvidenceStore(db_path)
    try:
        prompt_rows = store.prompts()
        attempt_rows = store.attempts()
    finally:
        store.close()

    # Redaction on unless the run explicitly turned it off: too much is the safe direction.
    scrub: Callable[[str], str] = redact if run.get("redact", True) else _keep
    stored: dict[str, dict[int, AttemptView]] = {}
    for row in attempt_rows:
        verdict, reasons = row.verdict, row.reasons
        if final and verdict is Verdict.NEEDS_REVIEW:
            verdict, reasons = Verdict.INCONCLUSIVE, (*reasons, NOT_REVIEWED_REASON)
        stored.setdefault(row.prompt_id, {})[row.repeat_index] = AttemptView(
            attempt_id=row.attempt_id,
            verdict=verdict,
            decided_by=row.decided_by,
            reasons=tuple(scrub(reason) for reason in reasons),
            hint=row.hint,
            reply=None if row.reply_text is None else scrub(row.reply_text),
            error_kind=row.error_kind,
            http_status=row.http_status,
            latency_ms=row.latency_ms,
            human_note=scrub(row.human_note),
        )

    prompts: list[PromptView] = []
    for prompt in prompt_rows:
        have = stored.get(prompt.prompt_id, {})
        attempts = [
            have.get(index) or _not_run(prompt.prompt_id, index)
            for index in sorted({*range(1, prompt.repeat + 1), *have})
        ]
        prompts.append(
            PromptView(
                prompt_id=prompt.prompt_id,
                category=prompt.category,
                verdict=worst(attempt.verdict for attempt in attempts),
                pass_if=prompt.pass_if,
                prompt_text=prompt.prompt_text,
                attempts=attempts,
            )
        )

    totals = dict.fromkeys(Verdict, 0)
    for prompt_view in prompts:
        totals[prompt_view.verdict] += 1
    pending = sum(
        attempt.verdict is Verdict.NEEDS_REVIEW for item in prompts for attempt in item.attempts
    )
    return ReportData(run, final, totals, _category_rows(prompts), prompts, pending)


def write_reports(run_dir: Path, *, final: bool) -> list[Path]:
    """Write ``report.html``, ``report.md`` and ``matrix.csv`` into the run folder.

    A draft (``final=False``) also writes ``review.csv``, the queue of attempts a person must
    judge. A final report never touches ``review.csv``: it is the file the reviewer filled in.
    Returns the paths written.
    """
    data = build_report_data(run_dir, final=final)
    written = [
        _write_text(run_dir / "report.html", _render("report.html.j2", data, html=True)),
        _write_text(run_dir / "report.md", _render("report.md.j2", data, html=False)),
        _write_csv(run_dir / "matrix.csv", MATRIX_COLUMNS, _matrix_rows(data)),
    ]
    if not final:
        written.append(_write_csv(run_dir / "review.csv", REVIEW_COLUMNS, _review_rows(data)))
    return written


def run_facts(run: dict[str, Any]) -> list[tuple[str, str]]:
    """The few ``run.json`` facts a report shows, as ``(label, text)`` pairs.

    Only these are shown. The endpoint block is not printed beyond its name.
    """
    endpoint = run.get("endpoint")
    name = endpoint.get("name") if isinstance(endpoint, dict) else None
    return [
        ("Endpoint", _fact(name)),
        ("Run", _fact(run.get("run_id"))),
        ("Started", _fact(run.get("started_at"))),
        ("Finished", _fact(run.get("finished_at"))),
        ("Status", _fact(run.get("status"))),
    ]


def verdict_label(verdict: Verdict, decided_by: Iterable[DecidedBy] = ()) -> str:
    """The word a report prints for a verdict. A Pass says who decided it: ``Pass (rule)``,
    ``Pass (human)`` or ``Pass (rule + human)`` (a prompt whose attempts were decided both ways)."""
    word = _VERDICT_WORDS[verdict]
    if verdict is not Verdict.PASS:
        return word
    sources = sorted({who.value for who in decided_by if who is not DecidedBy.NONE})
    return f"{word} ({' + '.join(sources)})" if sources else word


def banner_text(data: ReportData) -> str:
    if data.final:
        return "FINAL"
    return f"DRAFT — {data.pending} attempts awaiting review (review.csv)"


def _keep(text: str) -> str:
    return text


def _fact(value: object) -> str:
    return "—" if value is None or value == "" else str(value)


def _not_run(prompt_id: str, repeat_index: int) -> AttemptView:
    """An attempt that was planned but never stored: Inconclusive, never a pass."""
    return AttemptView(
        attempt_id=f"{prompt_id}#{repeat_index}",
        verdict=Verdict.INCONCLUSIVE,
        decided_by=DecidedBy.NONE,
        reasons=(NOT_RUN_REASON,),
        hint="",
        reply=None,
        error_kind="not_run",
        http_status=None,
        latency_ms=None,
        human_note="",
    )


def _category_rows(prompts: list[PromptView]) -> list[CategoryRow]:
    rows: list[CategoryRow] = []
    for code, name in OWASP_LLM_2025.items():
        counts = Counter(item.verdict for item in prompts if item.category == code)
        total = sum(counts.values())
        if total == 0:
            continue
        passed = counts[Verdict.PASS]
        rows.append(
            CategoryRow(
                code=code,
                name=name,
                total=total,
                passed=passed,
                failed=counts[Verdict.FAIL],
                inconclusive=counts[Verdict.INCONCLUSIVE],
                needs_review=counts[Verdict.NEEDS_REVIEW],
                pass_rate=round(100 * passed / total, 1),
            )
        )
    return rows


@cache
def _environment(*, html: bool) -> Environment:
    """Jinja environment for the HTML (autoescaped) or Markdown (not) templates.

    Markdown is plain text: autoescape would double-escape what :func:`md_cell` already made
    inert, so every dynamic value in ``report.md.j2`` goes through the ``md_cell`` filter.
    """
    env = Environment(
        loader=PackageLoader("suite", "templates"),
        autoescape=html,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["md_cell"] = md_cell
    env.filters["verdict_label"] = verdict_label
    return env


def _render(template: str, data: ReportData, *, html: bool) -> str:
    return (
        _environment(html=html)
        .get_template(template)
        .render(
            data=data,
            Verdict=Verdict,
            facts=run_facts(data.run),
            legend=VERDICT_LEGEND,
            banner=banner_text(data),
        )
    )


def _write_text(path: Path, text: str) -> Path:
    # newline="": no CRLF translation on Windows, so a reply's own line endings are kept as-is.
    path.write_text(text, encoding="utf-8", newline="")
    return path


def _write_csv(path: Path, header: tuple[str, ...], rows: Iterable[list[object]]) -> Path:
    # utf-8-sig puts a BOM first, so Excel opens the file as UTF-8.
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def _matrix_rows(data: ReportData) -> list[list[object]]:
    return [
        [
            csv_cell(attempt.attempt_id),
            csv_cell(prompt.prompt_id),
            csv_cell(prompt.category),
            csv_cell(attempt.verdict.value),
            csv_cell(attempt.decided_by.value),
            csv_cell("; ".join(attempt.reasons)),
            csv_cell(attempt.hint),
            csv_cell(attempt.error_kind or ""),
            "" if attempt.http_status is None else attempt.http_status,
            "" if attempt.latency_ms is None else attempt.latency_ms,
            csv_cell(_excerpt(attempt.reply)),
        ]
        for prompt in data.prompts
        for attempt in prompt.attempts
    ]


def _review_rows(data: ReportData) -> list[list[object]]:
    queued = [
        (prompt, attempt)
        for prompt in data.prompts
        for attempt in prompt.attempts
        if attempt.verdict is Verdict.NEEDS_REVIEW
    ]
    # Pack order, then repeat index, is the order already; the stable sort floats the replies
    # that did not look like a refusal (the likelier problems) to the top.
    queued.sort(key=lambda pair: pair[1].hint != "no_refusal")
    return [
        [
            csv_cell(attempt.attempt_id),
            csv_cell(prompt.prompt_id),
            csv_cell(prompt.category),
            csv_cell(attempt.hint),
            csv_cell(prompt.pass_if),
            csv_cell(prompt.prompt_text),
            csv_cell(attempt.reply or ""),
            "",
            "",
        ]
        for prompt, attempt in queued
    ]


def _excerpt(reply: str | None) -> str:
    """The first 200 characters of a reply, on one line."""
    if reply is None:
        return ""
    return reply[:EXCERPT_CHARS].replace("\r", " ").replace("\n", " ")
