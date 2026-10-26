import csv
import json
from pathlib import Path

import pytest

from suite.evidence import EvidenceStore, read_run_json, write_run_json
from suite.models import (
    Attempt,
    ConfigError,
    DecidedBy,
    Judgement,
    SendOutcome,
    Verdict,
)
from suite.report import (
    EXCERPT_CHARS,
    FORMULA_PREFIXES,
    build_report_data,
    csv_cell,
    md_cell,
    write_reports,
)
from tests.factories import RUN_STARTED, make_run_dir

LEGEND = (
    "Pass (rule) = every configured check held — it is not proof the bot is safe. "
    "Pass (human) = a reviewer judged the reply against pass_if. "
    "Fail = a check tripped or a reviewer marked it failed. "
    "Inconclusive = no usable reply, or a check could not run — never counted as a pass. "
    "Needs review = no automatic check; see review.csv."
)

MATRIX_COLUMNS = [
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
]
REVIEW_COLUMNS = [
    "attempt_id",
    "prompt_id",
    "category",
    "hint",
    "pass_if",
    "prompt",
    "reply",
    "verdict",
    "note",
]


def read_csv(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


def read_dicts(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def add_attempt(run_dir: Path, attempt_id: str, verdict: Verdict, reasons: tuple[str, ...]) -> None:
    """Store one more attempt in an existing run folder (to set reasons make_run_dir cannot)."""
    prompt_id, _, index = attempt_id.rpartition("#")
    outcome = SendOutcome(
        Attempt(attempt_id, prompt_id, int(index), "sent"), "a reply", None, 5, RUN_STARTED
    )
    store = EvidenceStore(run_dir / "evidence.db")
    try:
        store.add_attempt(outcome, Judgement(verdict, DecidedBy.RULE, reasons, ""))
    finally:
        store.close()


# --- the data model ----------------------------------------------------------------------------


def test_category_counts_and_inconclusive_not_pass(tmp_path: Path) -> None:
    d = make_run_dir(
        tmp_path,
        [("P1#1", "LLM01", Verdict.PASS, "fine"), ("P2#1", "LLM01", Verdict.INCONCLUSIVE, None)],
    )
    data = build_report_data(d, final=False)
    (row,) = data.categories
    assert (row.code, row.name) == ("LLM01", "Prompt Injection")
    assert (row.total, row.passed, row.failed) == (2, 1, 0)
    assert (row.inconclusive, row.needs_review) == (1, 0)
    assert row.pass_rate == 50.0
    assert data.totals[Verdict.PASS] == 1
    assert data.totals[Verdict.INCONCLUSIVE] == 1
    assert data.totals[Verdict.FAIL] == 0


def test_pass_rate_is_rounded_to_one_decimal(tmp_path: Path) -> None:
    d = make_run_dir(
        tmp_path,
        [
            ("P1#1", "LLM01", Verdict.PASS, "ok"),
            ("P2#1", "LLM01", Verdict.FAIL, "bad"),
            ("P3#1", "LLM01", Verdict.FAIL, "bad"),
        ],
    )
    assert build_report_data(d, final=False).categories[0].pass_rate == 33.3


def test_categories_only_if_present_in_owasp_order(tmp_path: Path) -> None:
    d = make_run_dir(
        tmp_path,
        [
            ("A#1", "LLM07", Verdict.PASS, "ok"),
            ("B#1", "LLM02", Verdict.PASS, "ok"),
            ("C#1", "LLM10", Verdict.FAIL, "bad"),
            ("D#1", "LLM02", Verdict.FAIL, "bad"),
        ],
    )
    cats = build_report_data(d, final=False).categories
    assert [c.code for c in cats] == ["LLM02", "LLM07", "LLM10"]
    assert [c.total for c in cats] == [2, 1, 1]
    assert cats[0].name == "Sensitive Information Disclosure"


def test_prompt_verdict_is_worst_attempt(tmp_path: Path) -> None:
    d = make_run_dir(
        tmp_path,
        [
            ("P1#1", "LLM01", Verdict.PASS, "ok"),
            ("P1#2", "LLM01", Verdict.FAIL, "bad"),
            ("P1#3", "LLM01", Verdict.PASS, "ok"),
        ],
    )
    (prompt,) = build_report_data(d, final=False).prompts
    assert prompt.verdict is Verdict.FAIL
    assert [a.verdict for a in prompt.attempts] == [Verdict.PASS, Verdict.FAIL, Verdict.PASS]
    assert prompt.pass_if == "The bot answers safely"
    assert prompt.prompt_text == "Test prompt P1"


def test_missing_attempts_count_inconclusive(tmp_path: Path) -> None:
    d = make_run_dir(tmp_path, [("P1#1", "LLM01", Verdict.PASS, "ok")], repeat={"P1": 3})
    data = build_report_data(d, final=False)
    (prompt,) = data.prompts
    assert prompt.verdict is Verdict.INCONCLUSIVE
    assert [a.attempt_id for a in prompt.attempts] == ["P1#1", "P1#2", "P1#3"]
    missing = prompt.attempts[1:]
    assert all(a.verdict is Verdict.INCONCLUSIVE for a in missing)
    assert all(a.reasons == ("not_run: no outcome recorded",) for a in missing)
    assert all(a.reply is None and a.error_kind == "not_run" for a in missing)
    assert data.totals[Verdict.INCONCLUSIVE] == 1 and data.totals[Verdict.PASS] == 0


def test_final_view_turns_pending_into_inconclusive(tmp_path: Path) -> None:
    d = make_run_dir(
        tmp_path,
        [("P1#1", "LLM01", Verdict.NEEDS_REVIEW, "a reply"), ("P2#1", "LLM01", Verdict.PASS, "ok")],
    )
    draft = build_report_data(d, final=False)
    assert draft.final is False and draft.pending == 1
    assert draft.totals[Verdict.NEEDS_REVIEW] == 1 and draft.totals[Verdict.INCONCLUSIVE] == 0
    assert draft.categories[0].needs_review == 1

    final = build_report_data(d, final=True)
    assert final.final is True and final.pending == 0
    assert final.totals[Verdict.NEEDS_REVIEW] == 0 and final.totals[Verdict.INCONCLUSIVE] == 1
    assert final.categories[0].needs_review == 0 and final.categories[0].inconclusive == 1
    attempt = final.prompts[0].attempts[0]
    assert attempt.verdict is Verdict.INCONCLUSIVE and "not reviewed" in attempt.reasons
    assert final.prompts[0].verdict is Verdict.INCONCLUSIVE


def test_human_decisions_survive_into_the_final_view(tmp_path: Path) -> None:
    d = make_run_dir(tmp_path, [("P1#1", "LLM01", Verdict.NEEDS_REVIEW, "a reply")])
    store = EvidenceStore(d / "evidence.db")
    try:
        store.set_human_verdict("P1#1", Verdict.PASS, "looked fine")
    finally:
        store.close()
    final = build_report_data(d, final=True)
    attempt = final.prompts[0].attempts[0]
    assert (attempt.verdict, attempt.decided_by) == (Verdict.PASS, DecidedBy.HUMAN)
    assert attempt.human_note == "looked fine" and "not reviewed" not in attempt.reasons


def test_redaction_follows_run_json_and_spares_prompt_text(tmp_path: Path) -> None:
    rows = [("P1#1", "LLM02", Verdict.FAIL, "mail bob@example.com")]
    on = build_report_data(make_run_dir(tmp_path / "on", rows), final=False)
    assert on.prompts[0].attempts[0].reply == "mail [REDACTED:email_address]"
    off = build_report_data(make_run_dir(tmp_path / "off", rows, redact=False), final=False)
    assert off.prompts[0].attempts[0].reply == "mail bob@example.com"


def test_reasons_and_prompt_text_redaction(tmp_path: Path) -> None:
    d = make_run_dir(tmp_path, [("P1#1", "LLM02", Verdict.PASS, "ok")], repeat={"P1": 2})
    add_attempt(d, "P1#2", Verdict.FAIL, ("leaked bob@example.com",))
    store = EvidenceStore(d / "evidence.db")
    try:
        store._conn.execute("UPDATE prompts SET prompt_text = 'ask carol@example.com'")
        store._conn.commit()
    finally:
        store.close()
    (prompt,) = build_report_data(d, final=False).prompts
    assert prompt.attempts[1].reasons == ("leaked [REDACTED:email_address]",)
    assert prompt.prompt_text == "ask carol@example.com"  # prompt text is never redacted


def test_missing_evidence_db_is_a_config_error(tmp_path: Path) -> None:
    d = make_run_dir(tmp_path, [("P1#1", "LLM01", Verdict.PASS, "ok")])
    (d / "evidence.db").unlink()
    with pytest.raises(ConfigError, match="evidence.db"):
        build_report_data(d, final=False)
    assert not (d / "evidence.db").exists()  # reading must not create an empty store


def test_missing_run_json_is_a_config_error(tmp_path: Path) -> None:
    d = make_run_dir(tmp_path, [("P1#1", "LLM01", Verdict.PASS, "ok")])
    (d / "run.json").unlink()
    with pytest.raises(ConfigError, match="run.json"):
        build_report_data(d, final=False)


# --- cell helpers ------------------------------------------------------------------------------


def test_csv_cell_neutralises_formula_prefixes() -> None:
    assert FORMULA_PREFIXES == ("=", "+", "-", "@", "\t", "\r")
    assert csv_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert csv_cell("-5") == "'-5"
    for prefix in FORMULA_PREFIXES:
        assert csv_cell(prefix + "x") == "'" + prefix + "x"
    assert csv_cell("ok") == "ok"
    assert csv_cell("") == ""


def test_md_cell_rules() -> None:
    assert md_cell("a | b\nc\r\nd") == "a \\| b c d"
    assert md_cell("<img src=x>") == "&lt;img src=x&gt;"
    assert md_cell("short") == "short"
    assert md_cell("x" * 200) == "x" * 200
    assert md_cell("x" * 201) == "x" * 200 + "…"
    assert md_cell("abcdef", limit=3) == "abc…"


def test_md_cell_never_cuts_an_escape_in_half() -> None:
    cut = md_cell("<" * 300, limit=10)
    assert cut == "&lt;&lt;…"  # 8 escaped characters fit; a third "&lt;" would not
    assert md_cell("|" * 300, limit=5) == "\\|\\|…"


@pytest.mark.parametrize("char", list("\\`*_[]()!#~|"))
def test_md_cell_backslash_escapes_markdown_metacharacters(char: str) -> None:
    assert md_cell(f"a{char}b") == f"a\\{char}b"


def test_md_cell_escapes_every_metacharacter_in_one_string() -> None:
    assert md_cell("\\`*_[]()!#~|<>") == "\\\\\\`\\*\\_\\[\\]\\(\\)\\!\\#\\~\\|&lt;&gt;"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("http://e.example", "http[:]//e.example"),
        ("https://e.example/a?b=1", "https[:]//e.example/a?b=1"),
        ("HTTPS://E.EXAMPLE", "HTTPS[:]//E.EXAMPLE"),
        ("Http://e", "Http[:]//e"),
        ("www.e.example", "www[.]e.example"),
        ("WWW.e.example", "WWW[.]e.example"),
        ("see https://a.example and www.b.example", "see https[:]//a.example and www[.]b.example"),
        ("not a url: http, www, https:", "not a url: http, www, https:"),
    ],
)
def test_md_cell_defangs_bare_urls(text: str, expected: str) -> None:
    assert md_cell(text) == expected


def test_md_cell_renders_image_and_link_syntax_inert() -> None:
    cell = md_cell("![x](https://e.example/a.png) [y](https://e.example)")
    assert cell == ("\\!\\[x\\]\\(https[:]//e.example/a.png\\) \\[y\\]\\(https[:]//e.example\\)")
    assert "![" not in cell and "](" not in cell and "https://" not in cell


def test_md_cell_truncation_counts_escaped_length_and_keeps_escapes_whole() -> None:
    assert md_cell("*" * 300, limit=5) == "\\*\\*…"  # two escapes fit (4); a third would make 6
    # "https[:]//" is one unit of 10 characters: it is dropped whole, never cut part-way.
    assert md_cell("ab" + "https://e", limit=11) == "ab…"
    assert md_cell("ab" + "https://e", limit=12) == "abhttps[:]//…"
    assert md_cell("www.e.example", limit=5) == "…"


# --- HTML --------------------------------------------------------------------------------------


def test_html_escapes_reply(tmp_path: Path) -> None:
    d = make_run_dir(tmp_path, [("P1#1", "LLM05", Verdict.FAIL, "<script>x</script>")])
    write_reports(d, final=False)
    html = (d / "report.html").read_text(encoding="utf-8")
    assert "&lt;script&gt;x&lt;/script&gt;" in html and "<script>x</script>" not in html
    assert "<link" not in html and 'src="http' not in html


def test_html_is_self_contained(tmp_path: Path) -> None:
    d = make_run_dir(tmp_path, [("P1#1", "LLM05", Verdict.FAIL, "plain")])
    write_reports(d, final=False)
    html = (d / "report.html").read_text(encoding="utf-8").lower()
    for forbidden in ("<script", "<link", "<img", "<iframe", "src=", "@import", "url(", "http://"):
        assert forbidden not in html
    assert "<style>" in html and "<meta charset=" in html


def test_html_never_marks_untrusted_text_safe(tmp_path: Path) -> None:
    d = make_run_dir(tmp_path, [("P1#1", "LLM05", Verdict.FAIL, '"><b onload=x>')])
    store = EvidenceStore(d / "evidence.db")
    try:
        store._conn.execute("UPDATE prompts SET prompt_text = '<i>p</i>', pass_if = '<u>q</u>'")
        store._conn.execute("UPDATE attempts SET reasons_json = '[\"<s>r</s>\"]'")
        store._conn.commit()
    finally:
        store.close()
    write_reports(d, final=False)
    html = (d / "report.html").read_text(encoding="utf-8")
    for tag in ("<b onload", "<i>", "<u>", "<s>"):
        assert tag not in html
    for escaped in ("&lt;i&gt;p&lt;/i&gt;", "&lt;u&gt;q&lt;/u&gt;", "&lt;s&gt;r&lt;/s&gt;"):
        assert escaped in html


def test_html_content(tmp_path: Path) -> None:
    d = make_run_dir(
        tmp_path,
        [
            ("P1#1", "LLM01", Verdict.PASS, "all good"),
            ("P2#1", "LLM07", Verdict.FAIL, "line one\nline two"),
            ("P3#1", "LLM07", Verdict.INCONCLUSIVE, None),
        ],
    )
    write_reports(d, final=False)
    html = (d / "report.html").read_text(encoding="utf-8")
    assert LEGEND in html
    assert "20261023-140509-t" in html  # run id
    assert "2026-10-23T14:05:09+00:00" in html and "2026-10-23T14:06:30+00:00" in html
    assert "complete" in html
    assert "Prompt Injection" in html and "System Prompt Leakage" in html
    assert "<details>" in html
    assert "line one\nline two" in html  # the full reply, line breaks kept
    assert "no reply" in html
    assert "The bot answers safely" in html and "Test prompt P2" in html
    for word in ("Pass", "Fail", "Inconclusive"):
        assert word in html


def test_banner_draft_and_final(tmp_path: Path) -> None:
    d = make_run_dir(
        tmp_path,
        [
            ("P1#1", "LLM01", Verdict.NEEDS_REVIEW, "r1"),
            ("P2#1", "LLM01", Verdict.NEEDS_REVIEW, "r2"),
            ("P3#1", "LLM01", Verdict.PASS, "ok"),
        ],
    )
    write_reports(d, final=False)
    draft_html = (d / "report.html").read_text(encoding="utf-8")
    draft_md = (d / "report.md").read_text(encoding="utf-8")
    assert "DRAFT — 2 attempts awaiting review (review.csv)" in draft_html
    assert "DRAFT — 2 attempts awaiting review (review.csv)" in draft_md
    assert "FINAL" not in draft_html and "FINAL" not in draft_md

    write_reports(d, final=True)
    final_html = (d / "report.html").read_text(encoding="utf-8")
    final_md = (d / "report.md").read_text(encoding="utf-8")
    assert "FINAL" in final_html and "FINAL" in final_md
    assert "DRAFT" not in final_html and "DRAFT" not in final_md
    assert "not reviewed" in final_html and "not reviewed" in final_md
    assert LEGEND in final_html and LEGEND in final_md


def test_header_values_never_shown(tmp_path: Path) -> None:
    d = make_run_dir(tmp_path, [("P1#1", "LLM01", Verdict.NEEDS_REVIEW, "r")])
    write_reports(d, final=False)
    for path in d.iterdir():
        assert b"s3cret-token" not in path.read_bytes(), path.name
    assert "Authorization" not in (d / "report.html").read_text(encoding="utf-8")


# --- Markdown ----------------------------------------------------------------------------------


def test_markdown_points_to_where_full_replies_really_are(tmp_path: Path) -> None:
    # matrix.csv holds only a 200-character excerpt, so it must not be named as the full copy.
    d = make_run_dir(tmp_path, [("P1#1", "LLM01", Verdict.PASS, "x" * (EXCERPT_CHARS + 50))])
    write_reports(d, final=False)
    md = (d / "report.md").read_text(encoding="utf-8")
    assert "Full replies are in report.html (and evidence.db)." in md
    assert "matrix.csv" not in md


def test_markdown_cells_inert(tmp_path: Path) -> None:
    d = make_run_dir(tmp_path, [("P1#1", "LLM05", Verdict.FAIL, "a | b\n<img src=x>")])
    write_reports(d, final=False)
    md = (d / "report.md").read_text(encoding="utf-8")
    assert "a \\| b &lt;img src=x&gt;" in md and "<img" not in md


def test_markdown_content(tmp_path: Path) -> None:
    d = make_run_dir(
        tmp_path,
        [
            ("P1#1", "LLM01", Verdict.PASS, "all good"),
            ("P2#1", "LLM07", Verdict.FAIL, "a reply"),
            ("P2#2", "LLM07", Verdict.PASS, "another"),
        ],
    )
    write_reports(d, final=False)
    md = (d / "report.md").read_text(encoding="utf-8")
    assert LEGEND in md
    assert "20261023-140509-t" in md and "2026-10-23T14:05:09+00:00" in md
    assert "| LLM01 | Prompt Injection | 1 | 1 | 0 | 0 | 0 | 100.0% |" in md
    assert "| LLM07 | System Prompt Leakage | 1 | 0 | 1 | 0 | 0 | 0.0% |" in md
    assert "| P2 | LLM07 | Fail |" in md
    assert "test: a check tripped" in md


def test_markdown_table_cells_cannot_break_the_table(tmp_path: Path) -> None:
    d = make_run_dir(tmp_path, [("P1#1", "LLM01", Verdict.FAIL, "x")])
    store = EvidenceStore(d / "evidence.db")
    try:
        store._conn.execute("UPDATE attempts SET reasons_json = '[\"a|b\\nc\"]'")
        store._conn.commit()
    finally:
        store.close()
    write_reports(d, final=False)
    md = (d / "report.md").read_text(encoding="utf-8")
    row = next(line for line in md.splitlines() if line.startswith("| P1 |"))
    assert "a\\|b c" in row


def test_markdown_replies_cannot_become_links_images_or_autolinks(tmp_path: Path) -> None:
    reply = "![x](https://e.example/a.png) [y](https://e.example) `code` *b* www.e.example"
    d = make_run_dir(tmp_path, [("P1#1", "LLM05", Verdict.FAIL, reply)])
    store = EvidenceStore(d / "evidence.db")
    try:
        store._conn.execute(
            "UPDATE attempts SET reasons_json = ?",
            (json.dumps(["see [z](https://e.example/r) and ![i](http://e.example/i.png) `x`"]),),
        )
        store._conn.commit()
    finally:
        store.close()
    run = read_run_json(d)
    run["endpoint"]["name"] = "[ep](https://e.example/ep) ![e](https://e.example/e.png)"
    write_run_json(d, run)

    write_reports(d, final=False)
    md = (d / "report.md").read_text(encoding="utf-8")
    assert "![" not in md  # no live image
    assert "](" not in md  # no live link
    assert "http://" not in md and "https://" not in md and "www." not in md.lower()
    assert "`" not in md.replace("\\`", "")  # no live code span
    # the text is still all there, readable once rendered
    assert "\\!\\[x\\]\\(https[:]//e.example/a.png\\)" in md
    assert "\\`code\\` \\*b\\* www[.]e.example" in md
    assert "see \\[z\\]\\(https[:]//e.example/r\\)" in md  # the reasons cell
    assert "\\[ep\\]\\(https[:]//e.example/ep\\)" in md  # the run facts


# --- CSV ---------------------------------------------------------------------------------------


def test_csv_cells_neutralised(tmp_path: Path) -> None:
    d = make_run_dir(tmp_path, [("P1#1", "LLM05", Verdict.FAIL, "=HYPERLINK(1)")])
    write_reports(d, final=False)
    (row,) = read_dicts(d / "matrix.csv")
    assert row["reply_excerpt"] == "'=HYPERLINK(1)"
    assert csv_cell("-5") == "'-5" and csv_cell("ok") == "ok"


@pytest.mark.parametrize("value", ["=1+1", "+cmd", "-2", "@SUM(A1)"])
def test_every_csv_text_cell_is_neutralised(tmp_path: Path, value: str) -> None:
    """A formula-leading value in any text field is defused in matrix.csv and review.csv."""
    d = make_run_dir(
        tmp_path,
        [("P1#1", "LLM01", Verdict.NEEDS_REVIEW, value)],
        hints={"P1#1": value},
    )
    # Fields the normal paths cannot fill with such a value (ids, category, error kind, reasons),
    # as a hand-edited or hostile evidence.db could: the report must still defuse them.
    attempt_id, prompt_id = value + "#1", value
    store = EvidenceStore(d / "evidence.db")
    try:
        conn = store._conn
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "UPDATE prompts SET prompt_id = ?, category = ?, prompt_text = ?, pass_if = ?",
            (prompt_id, value, value, value),
        )
        conn.execute(
            "UPDATE attempts SET attempt_id = ?, prompt_id = ?, error_kind = ?, reasons_json = ?",
            (attempt_id, prompt_id, value, json.dumps([value])),
        )
        conn.commit()
    finally:
        store.close()
    write_reports(d, final=False)

    expected = "'" + value
    (matrix,) = read_dicts(d / "matrix.csv")
    for column in ("prompt_id", "category", "reasons", "hint", "error_kind", "reply_excerpt"):
        assert matrix[column] == expected, f"matrix.csv {column}"
    assert matrix["attempt_id"] == "'" + attempt_id
    (review,) = read_dicts(d / "review.csv")
    for column in ("prompt_id", "category", "hint", "pass_if", "prompt", "reply"):
        assert review[column] == expected, f"review.csv {column}"
    assert review["attempt_id"] == "'" + attempt_id


def test_matrix_csv_columns_and_rows(tmp_path: Path) -> None:
    long_reply = "first line\nsecond " + "x" * 300
    d = make_run_dir(
        tmp_path,
        [
            ("P1#1", "LLM01", Verdict.PASS, "日本語 ok"),
            ("P2#1", "LLM07", Verdict.FAIL, long_reply),
            ("P3#1", "LLM02", Verdict.INCONCLUSIVE, None),
        ],
        hints={"P1#1": "refusal"},
        repeat={"P1": 2},
    )
    write_reports(d, final=False)
    raw = (d / "matrix.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = read_csv(d / "matrix.csv")
    assert rows[0] == MATRIX_COLUMNS
    by_id = {r["attempt_id"]: r for r in read_dicts(d / "matrix.csv")}
    assert list(by_id) == ["P1#1", "P1#2", "P2#1", "P3#1"]  # pack order, then repeat index
    assert by_id["P1#1"]["reply_excerpt"] == "日本語 ok"
    assert by_id["P1#1"]["hint"] == "refusal" and by_id["P1#1"]["decided_by"] == "rule"
    assert by_id["P1#1"]["latency_ms"] == "12"
    assert by_id["P1#2"]["verdict"] == "inconclusive"
    assert by_id["P1#2"]["reasons"] == "not_run: no outcome recorded"
    assert by_id["P1#2"]["error_kind"] == "not_run"
    excerpt = by_id["P2#1"]["reply_excerpt"]
    assert excerpt == ("first line second " + "x" * 300)[:200] and "\n" not in excerpt
    assert by_id["P2#1"]["category"] == "LLM07" and by_id["P2#1"]["verdict"] == "fail"
    assert by_id["P3#1"]["error_kind"] == "transport" and by_id["P3#1"]["reply_excerpt"] == ""
    assert by_id["P3#1"]["reasons"] == "transport: no reply"


def test_matrix_reasons_are_joined(tmp_path: Path) -> None:
    d = make_run_dir(tmp_path, [("P1#1", "LLM01", Verdict.PASS, "ok")], repeat={"P1": 2})
    add_attempt(d, "P1#2", Verdict.FAIL, ("first", "second"))
    write_reports(d, final=False)
    by_id = {r["attempt_id"]: r for r in read_dicts(d / "matrix.csv")}
    assert by_id["P1#2"]["reasons"] == "first; second"


def test_matrix_final_shows_not_reviewed(tmp_path: Path) -> None:
    d = make_run_dir(tmp_path, [("P1#1", "LLM01", Verdict.NEEDS_REVIEW, "r")])
    write_reports(d, final=False)
    assert read_dicts(d / "matrix.csv")[0]["verdict"] == "needs_review"
    write_reports(d, final=True)
    (row,) = read_dicts(d / "matrix.csv")
    assert row["verdict"] == "inconclusive" and row["reasons"] == "not reviewed"


def test_review_csv_rows_and_order(tmp_path: Path) -> None:
    d = make_run_dir(
        tmp_path,
        [
            ("P1#1", "LLM01", Verdict.NEEDS_REVIEW, "I cannot help with that"),
            ("P2#1", "LLM02", Verdict.NEEDS_REVIEW, "=SUM(1)\nsecond line"),
            ("P3#1", "LLM02", Verdict.PASS, "fine"),
            ("P4#1", "LLM02", Verdict.FAIL, "bad"),
            ("P5#1", "LLM02", Verdict.NEEDS_REVIEW, "another"),
            ("P5#2", "LLM02", Verdict.NEEDS_REVIEW, "again"),
        ],
        hints={"P1#1": "refusal", "P2#1": "no_refusal", "P5#2": "no_refusal"},
    )
    paths = write_reports(d, final=False)
    assert d / "review.csv" in paths
    raw = (d / "review.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert read_csv(d / "review.csv")[0] == REVIEW_COLUMNS
    rows = read_dicts(d / "review.csv")
    # no_refusal first (pack order, then repeat index), then the rest in pack order
    assert [r["attempt_id"] for r in rows] == ["P2#1", "P5#2", "P1#1", "P5#1"]
    first = rows[0]
    assert first["prompt_id"] == "P2" and first["category"] == "LLM02"
    assert first["hint"] == "no_refusal"
    assert first["pass_if"] == "The bot answers safely"
    assert first["prompt"] == "Test prompt P2"
    assert first["reply"] == "'=SUM(1)\nsecond line"  # full reply, neutralised
    assert first["verdict"] == "" and first["note"] == ""
    assert all(r["verdict"] == "" and r["note"] == "" for r in rows)


def test_review_csv_is_written_with_crlf_rows_and_no_blank_lines(tmp_path: Path) -> None:
    d = make_run_dir(tmp_path, [("P1#1", "LLM01", Verdict.NEEDS_REVIEW, "x")])
    write_reports(d, final=False)
    text = (d / "review.csv").read_bytes().decode("utf-8-sig")
    assert text.endswith("\r\n") and "\r\n\r\n" not in text and "\r\r" not in text


def test_review_csv_has_header_even_when_nothing_pends(tmp_path: Path) -> None:
    d = make_run_dir(tmp_path, [("P1#1", "LLM01", Verdict.PASS, "ok")])
    write_reports(d, final=False)
    assert read_csv(d / "review.csv") == [REVIEW_COLUMNS]


def test_redaction_applies_to_outputs_not_db(tmp_path: Path) -> None:
    d = make_run_dir(
        tmp_path,
        [
            ("P1#1", "LLM02", Verdict.FAIL, "mail bob@example.com"),
            ("P2#1", "LLM02", Verdict.NEEDS_REVIEW, "mail bob@example.com"),
        ],
    )
    write_reports(d, final=False)
    for name in ("report.html", "report.md", "matrix.csv", "review.csv"):
        assert "bob@example.com" not in (d / name).read_text(encoding="utf-8-sig"), name
    assert "[REDACTED:email_address]" in (d / "report.html").read_text(encoding="utf-8")
    store = EvidenceStore(d / "evidence.db")
    try:
        assert store.attempts()[0].reply_text == "mail bob@example.com"
    finally:
        store.close()


def test_redact_off_keeps_replies_in_outputs(tmp_path: Path) -> None:
    d = make_run_dir(
        tmp_path, [("P1#1", "LLM02", Verdict.FAIL, "mail bob@example.com")], redact=False
    )
    write_reports(d, final=False)
    assert "bob@example.com" in (d / "report.html").read_text(encoding="utf-8")
    assert "bob@example.com" in (d / "matrix.csv").read_text(encoding="utf-8-sig")


# --- write_reports -----------------------------------------------------------------------------


def test_write_reports_returns_what_it_wrote(tmp_path: Path) -> None:
    d = make_run_dir(tmp_path, [("P1#1", "LLM01", Verdict.NEEDS_REVIEW, "r")])
    draft = write_reports(d, final=False)
    assert [p.name for p in draft] == ["report.html", "report.md", "matrix.csv", "review.csv"]
    assert all(p.parent == d and p.is_file() for p in draft)
    final = write_reports(d, final=True)
    assert [p.name for p in final] == ["report.html", "report.md", "matrix.csv"]


def test_final_does_not_touch_review_csv(tmp_path: Path) -> None:
    d = make_run_dir(tmp_path, [("P1#1", "LLM01", Verdict.NEEDS_REVIEW, "r")])
    (d / "review.csv").write_text("sentinel", encoding="utf-8")
    write_reports(d, final=True)
    assert (d / "review.csv").read_text(encoding="utf-8") == "sentinel"


def test_final_does_not_create_review_csv(tmp_path: Path) -> None:
    d = make_run_dir(tmp_path, [("P1#1", "LLM01", Verdict.NEEDS_REVIEW, "r")])
    write_reports(d, final=True)
    assert not (d / "review.csv").exists()


def test_reports_survive_an_empty_run(tmp_path: Path) -> None:
    d = make_run_dir(tmp_path, [])
    write_reports(d, final=False)
    data = build_report_data(d, final=False)
    assert data.prompts == [] and data.categories == [] and data.pending == 0
    assert (d / "report.html").is_file() and (d / "report.md").is_file()
