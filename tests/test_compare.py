from pathlib import Path

import pytest

from suite.compare import Change, CompareRow, compare_runs, endpoint_name, write_compare
from suite.evidence import read_run_json, write_run_json
from suite.models import ConfigError, Verdict
from tests.factories import make_run_dir

COUNTS_LINE = "fixed 1 · regressed 1 · unchanged 1 · unclear 1 · new 1 · removed 1"


def one(prompt_id: str, verdict: Verdict, category: str = "LLM01") -> tuple[str, str, Verdict, str]:
    return (f"{prompt_id}#1", category, verdict, "a reply")


def two_runs(
    tmp_path: Path,
    before: list[tuple[str, str, Verdict, str | None]],
    after: list[tuple[str, str, Verdict, str | None]],
) -> tuple[Path, Path]:
    return make_run_dir(tmp_path / "a", before), make_run_dir(tmp_path / "b", after)


def set_endpoint_name(run_dir: Path, name: str) -> None:
    run = read_run_json(run_dir)
    run["endpoint"]["name"] = name
    write_run_json(run_dir, run)


def verdicts(rows: list[CompareRow]) -> list[tuple[str, Change]]:
    return [(row.prompt_id, row.change) for row in rows]


@pytest.mark.parametrize(
    "before,after,change",
    [
        (Verdict.FAIL, Verdict.PASS, Change.FIXED),
        (Verdict.PASS, Verdict.FAIL, Change.REGRESSED),
        (Verdict.PASS, Verdict.PASS, Change.UNCHANGED),
        (Verdict.FAIL, Verdict.FAIL, Change.UNCHANGED),
        (Verdict.INCONCLUSIVE, Verdict.INCONCLUSIVE, Change.UNCHANGED),
        (Verdict.FAIL, Verdict.INCONCLUSIVE, Change.UNCLEAR),
        (Verdict.NEEDS_REVIEW, Verdict.PASS, Change.UNCLEAR),
        # Inconclusive is never a pass, so getting out of it is not a fix, nor a regression.
        (Verdict.INCONCLUSIVE, Verdict.PASS, Change.UNCLEAR),
        (Verdict.PASS, Verdict.INCONCLUSIVE, Change.UNCLEAR),
        (Verdict.INCONCLUSIVE, Verdict.FAIL, Change.UNCLEAR),
        # Final view: a pending item counts as Inconclusive, on both sides.
        (Verdict.NEEDS_REVIEW, Verdict.NEEDS_REVIEW, Change.UNCHANGED),
        (Verdict.NEEDS_REVIEW, Verdict.INCONCLUSIVE, Change.UNCHANGED),
        (Verdict.FAIL, Verdict.NEEDS_REVIEW, Change.UNCLEAR),
    ],
)
def test_transitions(tmp_path: Path, before: Verdict, after: Verdict, change: Change) -> None:
    baseline, run = two_runs(tmp_path, [one("P-1", before)], [one("P-1", after)])
    [row] = compare_runs(baseline, run)
    assert row == CompareRow(
        "P-1",
        "LLM01",
        Verdict.INCONCLUSIVE if before is Verdict.NEEDS_REVIEW else before,
        Verdict.INCONCLUSIVE if after is Verdict.NEEDS_REVIEW else after,
        change,
    )


def test_change_values_are_lower_case() -> None:
    assert [change.value for change in Change] == [
        "fixed",
        "regressed",
        "unchanged",
        "unclear",
        "new",
        "removed",
    ]


def test_compares_the_worst_verdict_over_a_prompts_attempts(tmp_path: Path) -> None:
    baseline, run = two_runs(
        tmp_path,
        [("P-1#1", "LLM01", Verdict.PASS, "ok"), ("P-1#2", "LLM01", Verdict.FAIL, "ok")],
        [("P-1#1", "LLM01", Verdict.PASS, "ok"), ("P-1#2", "LLM01", Verdict.PASS, "ok")],
    )
    [row] = compare_runs(baseline, run)
    assert (row.baseline, row.current, row.change) == (Verdict.FAIL, Verdict.PASS, Change.FIXED)


def test_an_attempt_that_never_ran_is_inconclusive_not_a_pass(tmp_path: Path) -> None:
    baseline = make_run_dir(tmp_path / "a", [one("P-1", Verdict.FAIL)])
    run = make_run_dir(tmp_path / "b", [one("P-1", Verdict.PASS)], repeat={"P-1": 2})
    [row] = compare_runs(baseline, run)
    assert (row.current, row.change) == (Verdict.INCONCLUSIVE, Change.UNCLEAR)


def test_new_and_removed(tmp_path: Path) -> None:
    baseline, run = two_runs(
        tmp_path,
        [
            one("P-A", Verdict.PASS),
            one("P-B", Verdict.FAIL),
            one("P-C", Verdict.PASS, "LLM02"),
            one("P-D", Verdict.FAIL, "LLM07"),
        ],
        [one("P-B", Verdict.PASS), one("P-N", Verdict.FAIL, "LLM05"), one("P-A", Verdict.PASS)],
    )
    rows = compare_runs(baseline, run)
    # The new run's prompt order first, then the removed rows in the baseline's order.
    assert rows == [
        CompareRow("P-B", "LLM01", Verdict.FAIL, Verdict.PASS, Change.FIXED),
        CompareRow("P-N", "LLM05", None, Verdict.FAIL, Change.NEW),
        CompareRow("P-A", "LLM01", Verdict.PASS, Verdict.PASS, Change.UNCHANGED),
        CompareRow("P-C", "LLM02", Verdict.PASS, None, Change.REMOVED),
        CompareRow("P-D", "LLM07", Verdict.FAIL, None, Change.REMOVED),
    ]


def test_a_moved_prompt_takes_the_new_runs_category(tmp_path: Path) -> None:
    baseline, run = two_runs(
        tmp_path, [one("P-1", Verdict.PASS, "LLM01")], [one("P-1", Verdict.PASS, "LLM02")]
    )
    [row] = compare_runs(baseline, run)
    assert row.category == "LLM02"


def test_the_same_folder_is_refused(tmp_path: Path) -> None:
    run = make_run_dir(tmp_path, [one("P-1", Verdict.PASS)])
    with pytest.raises(ConfigError, match="baseline and run are the same folder"):
        compare_runs(run, run)
    # A different spelling of the same folder is the same folder.
    with pytest.raises(ConfigError, match="baseline and run are the same folder"):
        compare_runs(run, run / ".." / run.name)


def test_a_run_without_evidence_is_a_config_error(tmp_path: Path) -> None:
    baseline, run = two_runs(tmp_path, [one("P-1", Verdict.PASS)], [one("P-1", Verdict.PASS)])
    (run / "evidence.db").unlink()
    with pytest.raises(ConfigError, match="evidence.db"):
        compare_runs(baseline, run)


def test_compare_runs_prints_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    baseline, run = two_runs(tmp_path, [one("P-1", Verdict.FAIL)], [one("P-1", Verdict.PASS)])
    compare_runs(baseline, run)
    assert capsys.readouterr() == ("", "")


def test_write_compare_md(tmp_path: Path) -> None:
    baseline, run = two_runs(
        tmp_path,
        [
            one("P-fixed", Verdict.FAIL),
            one("P-regressed", Verdict.PASS),
            one("P-same", Verdict.PASS),
            one("P-unclear", Verdict.FAIL),
            one("P-gone", Verdict.PASS, "LLM02"),
        ],
        [
            one("P-fixed", Verdict.PASS),
            one("P-regressed", Verdict.FAIL),
            one("P-same", Verdict.PASS),
            one("P-unclear", Verdict.INCONCLUSIVE),
            one("P-fresh", Verdict.FAIL, "LLM05"),
        ],
    )
    before_files = {path.name: path.read_bytes() for path in run.iterdir()}

    path = write_compare(run, compare_runs(baseline, run), baseline_id="base-run-id")

    assert path == run / "compare.md"
    # Only compare.md is new: nothing else in the run folder is touched.
    assert {p.name: p.read_bytes() for p in run.iterdir() if p.name != "compare.md"} == before_files
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == f"# Compare: base-run-id → {run.name}"
    assert COUNTS_LINE in lines
    table = [line for line in lines if line.startswith("|")]
    assert table == [
        "| Prompt | Category | Baseline | This run | Change |",
        "| --- | --- | --- | --- | --- |",
        "| P-fixed | LLM01 | Fail | Pass | fixed |",
        "| P-regressed | LLM01 | Pass | Fail | regressed |",
        "| P-unclear | LLM01 | Fail | Inconclusive | unclear |",
        "| P-fresh | LLM05 | — | Fail | new |",
        "| P-gone | LLM02 | Pass | — | removed |",
    ]
    assert "P-same" not in text
    assert "different endpoints" not in text


def test_the_counts_line_counts_every_row_including_unchanged(tmp_path: Path) -> None:
    baseline, run = two_runs(
        tmp_path,
        [one("P-1", Verdict.PASS), one("P-2", Verdict.PASS), one("P-3", Verdict.FAIL)],
        [one("P-1", Verdict.PASS), one("P-2", Verdict.PASS), one("P-3", Verdict.PASS)],
    )
    path = write_compare(run, compare_runs(baseline, run), baseline_id="b")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert "fixed 1 · regressed 0 · unchanged 2 · unclear 0 · new 0 · removed 0" in lines


def test_no_table_when_nothing_changed(tmp_path: Path) -> None:
    baseline, run = two_runs(tmp_path, [one("P-1", Verdict.PASS)], [one("P-1", Verdict.PASS)])
    text = write_compare(run, compare_runs(baseline, run), baseline_id="b").read_text(
        encoding="utf-8"
    )
    assert "fixed 0 · regressed 0 · unchanged 1 · unclear 0 · new 0 · removed 0" in text
    assert "No prompt changed." in text
    assert "| Prompt |" not in text


def test_notes_when_the_runs_used_different_endpoints(tmp_path: Path) -> None:
    baseline, run = two_runs(tmp_path, [one("P-1", Verdict.FAIL)], [one("P-1", Verdict.PASS)])
    set_endpoint_name(baseline, "lab-bot-open")
    set_endpoint_name(run, "lab-bot-guarded")
    rows = compare_runs(baseline, run)

    different = write_compare(
        run, rows, baseline_id="b", baseline_endpoint=endpoint_name(read_run_json(baseline))
    )
    lines = different.read_text(encoding="utf-8").splitlines()
    assert "Note: these runs used different endpoints (lab-bot-open vs lab-bot-guarded)." in lines

    same = write_compare(run, rows, baseline_id="b", baseline_endpoint="lab-bot-guarded")
    assert "different endpoints" not in same.read_text(encoding="utf-8")


def test_values_from_a_run_are_inert_in_markdown(tmp_path: Path) -> None:
    hostile = "![x](https://evil.example/p.png)"
    baseline, run = two_runs(
        tmp_path,
        [one("P_1.x", Verdict.FAIL, "LLM01")],
        [one("P_1.x", Verdict.PASS, "LLM01")],
    )
    set_endpoint_name(run, hostile)
    run_json = read_run_json(run)
    run_json["run_id"] = "<b>run</b>"
    write_run_json(run, run_json)

    text = write_compare(
        run,
        compare_runs(baseline, run),
        baseline_id="[base](https://evil.example/b)",
        baseline_endpoint="a_b",
    ).read_text(encoding="utf-8")

    assert "https://" not in text
    assert "<b>" not in text
    assert "![" not in text
    assert "| P\\_1.x | LLM01 |" in text
    assert "\\<" not in text and "&lt;b&gt;run&lt;/b&gt;" in text
    assert "\\[base\\]" in text
    assert "(a\\_b vs \\!\\[x\\]\\(https[:]//evil.example/p.png\\))" in text


def test_endpoint_name_reads_the_name_from_run_json() -> None:
    assert endpoint_name({"endpoint": {"name": "lab-bot"}}) == "lab-bot"
    assert endpoint_name({"endpoint": {"name": ""}}) is None
    assert endpoint_name({"endpoint": {}}) is None
    assert endpoint_name({"endpoint": "lab-bot"}) is None
    assert endpoint_name({}) is None
