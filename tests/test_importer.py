"""The importer: garak datasets into OWASP baseline pack files plus COVERAGE.md.

Every test drives the importer from the benign fixture dataset (placeholder prompts, no attack
text) through ``base_dir``. ``import_source`` is pointed at it by swapping ``GARAK_MAP`` for a
one-row map, so no real garak file is read.
"""

import dataclasses
from importlib import metadata
from pathlib import Path

import pytest
import yaml

import suite.importer as importer
from suite.adapters.datasets import DatasetPrompt, load_garak_dataset
from suite.importer import ImportResult, build_prompts, import_source
from suite.importer_map import COVERED, MapRow
from suite.models import ConfigError
from suite.prompts import OWASP_LLM_2025, load_pack

FIX = Path(__file__).parent / "fixtures" / "garak"
DATASET = "fixture_extraction"
CAN = "SYSTEM-CANARY-7Q4P"
PASS_IF = "The bot reveals nothing it should not."
ROW = MapRow(
    dataset=DATASET,
    category="LLM07",
    detectors=("credential_leak",),
    canary_check=True,
    default_pass_if=PASS_IF,
)


@pytest.fixture
def fixture_map(monkeypatch: pytest.MonkeyPatch) -> dict[str, MapRow]:
    """``import_source`` sees only the benign fixture dataset."""
    row_map = {DATASET: ROW}
    monkeypatch.setattr(importer, "GARAK_MAP", row_map)
    return row_map


@pytest.fixture
def rows() -> list[DatasetPrompt]:
    return load_garak_dataset(DATASET, base_dir=FIX)


def _files_in(folder: Path) -> set[str]:
    return {p.name for p in folder.iterdir()}


# --- build_prompts ---------------------------------------------------------------------------


def test_builds_ids_category_and_checks(rows):
    ps = build_prompts(rows, ROW, canary=CAN, repeat=3)
    assert [p.id for p in ps] == [f"BASE-LLM07-{DATASET}-00{n}" for n in (1, 2, 3)]
    assert ps[0].category == "LLM07" and ps[0].repeat == 3
    assert ps[0].prompt == rows[0].text
    assert ps[0].checks.must_not_contain == [CAN]
    assert ps[0].checks.detectors == ["credential_leak"]
    assert ps[0].pass_if == PASS_IF


def test_without_canary_keeps_credential_leak(rows):
    ps = build_prompts(rows, ROW, canary=None, repeat=1)
    assert ps[0].checks.must_not_contain == [] and ps[0].checks.detectors == ["credential_leak"]
    assert not ps[0].checks.is_empty()  # still judged by the detector, not sent to review


def test_a_row_without_the_canary_check_never_gets_the_marker(rows):
    no_canary_row = dataclasses.replace(ROW, canary_check=False)
    ps = build_prompts(rows, no_canary_row, canary=CAN, repeat=1)
    assert all(p.checks.must_not_contain == [] for p in ps)


def test_a_row_with_no_detector_and_no_canary_routes_to_review(rows):
    bare_row = dataclasses.replace(ROW, detectors=())
    ps = build_prompts(rows, bare_row, canary=None, repeat=1)
    assert all(p.checks.is_empty() for p in ps)  # no check at all: the reviewer decides


def test_an_unloadable_prompt_names_the_dataset_and_index_not_the_text():
    bad = [
        DatasetPrompt(dataset=DATASET, source="", text="A fine prompt.", index=0),
        DatasetPrompt(dataset=DATASET, source="", text="", index=1),
    ]
    with pytest.raises(ConfigError, match=rf"{DATASET}.*index 1"):
        build_prompts(bad, ROW, canary=None, repeat=1)


@pytest.mark.parametrize("repeat", [0, 21])
def test_a_repeat_the_pack_would_refuse_is_reported(rows, repeat):
    with pytest.raises(ConfigError, match=rf"{DATASET}.*index 0.*repeat"):
        build_prompts(rows, ROW, canary=None, repeat=repeat)


# --- import_source ---------------------------------------------------------------------------


def test_import_writes_pack_and_coverage(tmp_path, fixture_map, rows):
    out = tmp_path / "baseline"
    result = import_source("garak", out, canary=CAN, repeat=2, base_dir=FIX)

    assert isinstance(result, ImportResult)
    assert result.out_dir == out
    assert {f.name for f in result.files} == {"LLM07.yaml", "COVERAGE.md"}
    assert all(f.parent == out and f.is_file() for f in result.files)
    assert _files_in(out) == {"LLM07.yaml", "COVERAGE.md"}  # no temp files left behind
    assert result.prompt_count == len(rows)
    assert result.categories == ["LLM07"]

    pack = load_pack(out)
    assert [p.id for p in pack.prompts] == [f"BASE-LLM07-{DATASET}-00{n}" for n in (1, 2, 3)]
    assert [p.prompt for p in pack.prompts] == [r.text for r in rows]  # sent as garak wrote it
    assert all(p.repeat == 2 and p.category == "LLM07" for p in pack.prompts)
    assert all(p.checks.must_not_contain == [CAN] for p in pack.prompts)
    assert all(p.checks.detectors == ["credential_leak"] for p in pack.prompts)
    assert all(p.pass_if == PASS_IF for p in pack.prompts)


def test_import_without_a_canary_still_judges_by_detector(tmp_path, fixture_map):
    import_source("garak", tmp_path, canary=None, base_dir=FIX)
    pack = load_pack(tmp_path)
    assert all(p.checks.must_not_contain == [] for p in pack.prompts)
    assert all(p.checks.detectors == ["credential_leak"] for p in pack.prompts)


def test_import_creates_the_output_folder(tmp_path, fixture_map):
    out = tmp_path / "deep" / "baseline"
    import_source("garak", out, canary=CAN, base_dir=FIX)
    assert (out / "LLM07.yaml").is_file()


def test_pack_file_starts_with_a_provenance_header(tmp_path, fixture_map):
    import_source("garak", tmp_path, canary=CAN, base_dir=FIX)
    text = (tmp_path / "LLM07.yaml").read_text(encoding="utf-8")
    comments = []
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        comments.append(line)
    assert comments, "the file must open with a comment header"
    block = "\n".join(comments)
    assert "garak" in block
    assert f"PyRIT {metadata.version('pyrit')}" in block
    assert "Apache-2.0" in block
    assert "git-ignored" in block and "regenerate, don't edit" in block
    assert DATASET in block
    assert "https://example.invalid/benign-fixture" in block  # the dataset's own source URL
    # A comment is invisible to the loader: the body is a plain top-level list of prompts.
    body = yaml.safe_load(text)
    assert isinstance(body, list) and body[0]["id"] == f"BASE-LLM07-{DATASET}-001"


def test_import_is_deterministic(tmp_path, fixture_map):
    a, b = tmp_path / "a", tmp_path / "b"
    import_source("garak", a, canary=CAN, repeat=2, base_dir=FIX)
    import_source("garak", b, canary=CAN, repeat=2, base_dir=FIX)
    assert (a / "LLM07.yaml").read_bytes() == (b / "LLM07.yaml").read_bytes()
    assert (a / "COVERAGE.md").read_bytes() == (b / "COVERAGE.md").read_bytes()


def test_a_rerun_over_the_same_folder_is_byte_identical(tmp_path, fixture_map):
    import_source("garak", tmp_path, canary=CAN, base_dir=FIX)
    first = {name: (tmp_path / name).read_bytes() for name in _files_in(tmp_path)}
    import_source("garak", tmp_path, canary=CAN, base_dir=FIX)
    assert {name: (tmp_path / name).read_bytes() for name in _files_in(tmp_path)} == first


def test_the_files_are_utf8_with_unix_newlines(tmp_path, fixture_map):
    import_source("garak", tmp_path, canary=CAN, base_dir=FIX)
    for name in ("LLM07.yaml", "COVERAGE.md"):
        data = (tmp_path / name).read_bytes()
        data.decode("utf-8")
        assert b"\r" not in data


def test_prompt_text_survives_the_yaml_round_trip_exactly(tmp_path, fixture_map):
    # Awkward but benign text: quotes, colons, a YAML-looking word, a leading dash, unicode, and
    # a newline. The importer must write it so load_pack reads back the very same string.
    awkward = ["yes", "- a: b", 'say "hi": # not a comment', "café – naïve", "a\nb"]
    body = {"dataset_name": DATASET, "seeds": [{"value": text} for text in awkward]}
    folder = tmp_path / "ds"
    folder.mkdir()
    (folder / f"{DATASET}.prompt").write_text(yaml.safe_dump(body), encoding="utf-8")
    expected = [r.text for r in load_garak_dataset(DATASET, base_dir=folder)]

    out = tmp_path / "out"
    import_source("garak", out, canary=None, base_dir=folder)
    assert [p.prompt for p in load_pack(out).prompts] == expected


def test_write_is_atomic(tmp_path, fixture_map, monkeypatch):
    out = tmp_path / "baseline"
    out.mkdir()
    (out / "LLM07.yaml").write_text("previous: content\n", encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk went away")

    monkeypatch.setattr(importer.os, "replace", boom)
    with pytest.raises(OSError, match="disk went away"):
        import_source("garak", out, canary=CAN, base_dir=FIX)

    assert (out / "LLM07.yaml").read_text(encoding="utf-8") == "previous: content\n"
    assert _files_in(out) == {"LLM07.yaml"}  # no partial file, no stray temp file


def test_a_failed_import_writes_nothing(tmp_path, fixture_map):
    folder = tmp_path / "ds"
    folder.mkdir()
    # The second prompt is empty: the loader accepts it, the pack validation refuses it.
    (folder / f"{DATASET}.prompt").write_text(
        'seeds:\n  - value: A fine prompt.\n  - value: ""\n', encoding="utf-8"
    )
    out = tmp_path / "out"
    with pytest.raises(ConfigError, match=rf"{DATASET}.*index 1"):
        import_source("garak", out, canary=CAN, base_dir=folder)
    assert not out.exists()  # nothing written, not even the folder


def test_a_missing_dataset_is_reported_and_writes_nothing(tmp_path, fixture_map):
    out = tmp_path / "out"
    with pytest.raises(ConfigError, match=DATASET):
        import_source("garak", out, canary=CAN, base_dir=tmp_path / "no-such-folder")
    assert not out.exists()


@pytest.mark.parametrize("canary", ["ab", "", "!!!---", "ABC-12"])
def test_short_canary_rejected(tmp_path, fixture_map, canary):
    # Fewer than 6 ASCII letters/digits once squashed (the way the judge matches).
    out = tmp_path / "out"
    with pytest.raises(ConfigError, match="canary"):
        import_source("garak", out, canary=canary, base_dir=FIX)
    assert not out.exists()


def test_a_canary_of_six_letters_or_digits_is_accepted(tmp_path, fixture_map):
    import_source("garak", tmp_path, canary="ABC-123", base_dir=FIX)
    assert load_pack(tmp_path).prompts[0].checks.must_not_contain == ["ABC-123"]


def test_unknown_source_rejected(tmp_path, fixture_map):
    out = tmp_path / "out"
    with pytest.raises(ConfigError, match="nope"):
        import_source("nope", out, canary=CAN, base_dir=FIX)
    assert not out.exists()


@pytest.mark.parametrize("name", ["../evil", "a/b", "a\\b", "..", "x..y", "/abs"])
def test_a_dataset_name_that_could_leave_the_folder_is_refused(tmp_path, monkeypatch, name):
    monkeypatch.setattr(importer, "GARAK_MAP", {name: dataclasses.replace(ROW, dataset=name)})
    out = tmp_path / "out"
    with pytest.raises(ConfigError, match="dataset name"):
        import_source("garak", out, canary=CAN, base_dir=FIX)
    assert not out.exists()


def test_two_datasets_in_one_category_share_a_file_in_id_order(tmp_path, monkeypatch):
    folder = tmp_path / "ds"
    folder.mkdir()
    for name, text in (("zeta_set", "Zeta one."), ("alpha_set", "Alpha one.")):
        (folder / f"{name}.prompt").write_text(
            f"dataset_name: {name}\nseeds:\n  - value: {text}\n", encoding="utf-8"
        )
    monkeypatch.setattr(
        importer,
        "GARAK_MAP",
        {
            "zeta_set": dataclasses.replace(ROW, dataset="zeta_set"),
            "alpha_set": dataclasses.replace(ROW, dataset="alpha_set"),
        },
    )
    out = tmp_path / "out"
    result = import_source("garak", out, canary=CAN, base_dir=folder)
    assert result.categories == ["LLM07"] and result.prompt_count == 2
    ids = [p.id for p in load_pack(out).prompts]
    assert ids == ["BASE-LLM07-alpha_set-001", "BASE-LLM07-zeta_set-001"]


def test_each_category_gets_its_own_file(tmp_path, monkeypatch):
    folder = tmp_path / "ds"
    folder.mkdir()
    for name in ("one_set", "two_set"):
        (folder / f"{name}.prompt").write_text(
            f"dataset_name: {name}\nseeds:\n  - value: A harmless question.\n", encoding="utf-8"
        )
    monkeypatch.setattr(
        importer,
        "GARAK_MAP",
        {
            "two_set": dataclasses.replace(ROW, dataset="two_set", category="LLM09"),
            "one_set": dataclasses.replace(ROW, dataset="one_set", category="LLM07"),
        },
    )
    out = tmp_path / "out"
    result = import_source("garak", out, canary=None, base_dir=folder)
    assert result.categories == ["LLM07", "LLM09"]
    assert [f.name for f in result.files] == ["LLM07.yaml", "LLM09.yaml", "COVERAGE.md"]
    assert {p.category for p in load_pack(out).prompts} == {"LLM07", "LLM09"}


def test_an_empty_map_is_an_error_not_an_empty_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "GARAK_MAP", {})
    out = tmp_path / "out"
    with pytest.raises(ConfigError, match="no datasets"):
        import_source("garak", out, canary=CAN, base_dir=FIX)
    assert not out.exists()


def test_a_stale_pack_file_with_a_clashing_id_fails_the_self_check(tmp_path, fixture_map):
    clash = f"- id: BASE-LLM07-{DATASET}-001\n  category: LLM07\n  prompt: Another prompt.\n"
    (tmp_path / "AAA-other.yaml").write_text(clash, encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate prompt id"):
        import_source("garak", tmp_path, canary=CAN, base_dir=FIX)


# --- COVERAGE.md -----------------------------------------------------------------------------


def test_coverage_lists_every_owasp_category_with_its_note(tmp_path, fixture_map):
    import_source("garak", tmp_path, canary=CAN, base_dir=FIX)
    text = (tmp_path / "COVERAGE.md").read_text(encoding="utf-8")
    for code, name in OWASP_LLM_2025.items():
        line = next(line for line in text.splitlines() if line.startswith(f"| {code} "))
        assert name in line
        assert COVERED[code] in line
    assert "git-ignored" in text and "regenerate, don't edit" in text


def test_coverage_states_the_four_uncovered_categories(tmp_path, fixture_map):
    import_source("garak", tmp_path, canary=CAN, base_dir=FIX)
    text = (tmp_path / "COVERAGE.md").read_text(encoding="utf-8")
    for code in ("LLM03", "LLM04", "LLM08", "LLM10"):
        line = next(line for line in text.splitlines() if line.startswith(f"| {code} "))
        assert "not covered" in line.lower()


def test_coverage_reports_what_this_run_imported(tmp_path, fixture_map):
    import_source("garak", tmp_path, canary=CAN, base_dir=FIX)
    text = (tmp_path / "COVERAGE.md").read_text(encoding="utf-8")
    assert f"| LLM07.yaml | LLM07 | {DATASET} | 3 |" in text


def test_coverage_pipes_cannot_break_the_table(tmp_path, fixture_map, monkeypatch):
    monkeypatch.setattr(importer, "COVERED", {**COVERED, "LLM01": "a | b"})
    import_source("garak", tmp_path, canary=CAN, base_dir=FIX)
    line = next(
        line
        for line in (tmp_path / "COVERAGE.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("| LLM01 ")
    )
    assert "a \\| b" in line
