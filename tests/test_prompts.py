from pathlib import Path

import pytest
import yaml

from suite.models import ConfigError
from suite.prompts import (
    DETECTOR_NAMES,
    OWASP_LLM_2025,
    Checks,
    Prompt,
    load_pack,
    load_packs,
)
from tests.factories import make_pack, make_prompt

CSV_HEADER = "id,category,prompt,repeat,must_not_contain,must_not_match,detectors,pass_if"


def write(tmp_path: Path, name: str, text: str, encoding: str = "utf-8") -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return path


def write_prompts(tmp_path: Path, name: str, *items: dict) -> Path:
    return write(tmp_path, name, yaml.safe_dump(list(items), sort_keys=False))


def item(pid: str = "X-1", **fields) -> dict:
    base = {"id": pid, "category": "LLM01", "prompt": "hello"}
    base.update(fields)
    return base


def test_owasp_titles_and_detector_names():
    assert list(OWASP_LLM_2025) == [f"LLM{n:02d}" for n in range(1, 11)]
    assert OWASP_LLM_2025["LLM01"] == "Prompt Injection"
    assert OWASP_LLM_2025["LLM07"] == "System Prompt Leakage"
    assert OWASP_LLM_2025["LLM10"] == "Unbounded Consumption"
    assert {"credential_leak", "xss_output", "markdown_injection"} == DETECTOR_NAMES


def test_loads_yaml_dir_in_file_then_list_order(tmp_path):
    pack_dir = tmp_path / "mypack"
    pack_dir.mkdir()
    write_prompts(pack_dir, "a.yaml", item("A2"), item("A1"))
    write_prompts(pack_dir, "b.yaml", item("B1"))
    pack = load_pack(pack_dir)
    assert [p.id for p in pack.prompts] == ["A2", "A1", "B1"]
    assert pack.name == "mypack"


def test_loads_a_single_file_and_names_the_pack_after_its_stem(tmp_path):
    path = write_prompts(tmp_path, "core.yaml", item("C-1", repeat=3, pass_if="refuses"))
    pack = load_pack(path)
    assert pack.name == "core"
    assert pack.prompts[0].repeat == 3 and pack.prompts[0].pass_if == "refuses"


def test_dir_loads_yaml_yml_and_csv_sorted_by_name_and_ignores_the_rest(tmp_path):
    pack_dir = tmp_path / "p"
    (pack_dir / "nested").mkdir(parents=True)
    write_prompts(pack_dir / "nested", "deep.yaml", item("DEEP"))
    write_prompts(pack_dir, "c.yml", item("C"))
    write(pack_dir, "b.csv", f"{CSV_HEADER}\nB,LLM02,hi,,,,,\n")
    write_prompts(pack_dir, "a.yaml", item("A"))
    write(pack_dir, "notes.txt", "not a pack file")
    assert [p.id for p in load_pack(pack_dir).prompts] == ["A", "B", "C"]


def test_loads_csv_with_list_cells_and_bom(tmp_path):
    text = (
        f"{CSV_HEADER}\n"
        'CSV-1,LLM02,Say hello,,SYSTEM-CANARY-7Q4P,"email_address|re:\\d{4}",'
        "credential_leak|xss_output,Bot refuses\n"
        "CSV-2,LLM01,Second,3,,,,\n"
    )
    pack = load_pack(write(tmp_path, "p.csv", text, encoding="utf-8-sig"))
    first, second = pack.prompts
    assert first.checks.must_not_match == ["email_address", r"re:\d{4}"]
    assert first.checks.must_not_contain == ["SYSTEM-CANARY-7Q4P"]
    assert first.checks.detectors == ["credential_leak", "xss_output"]
    assert first.repeat == 1 and first.pass_if == "Bot refuses"
    assert second.repeat == 3 and second.checks.is_empty() and second.pass_if == ""


def test_csv_list_cells_ignore_spaces_around_pipes_and_empty_items(tmp_path):
    text = f"{CSV_HEADER}\nC-1,LLM01,hi,,,email_address | phone_number |,,\n"
    pack = load_pack(write(tmp_path, "p.csv", text))
    assert pack.prompts[0].checks.must_not_match == ["email_address", "phone_number"]


def test_csv_may_omit_optional_columns(tmp_path):
    pack = load_pack(write(tmp_path, "p.csv", "id,category,prompt\nC-1,LLM01,hi\n"))
    assert pack.prompts[0] == make_prompt(id="C-1", text="hi")


def test_csv_header_problems_are_reported(tmp_path):
    missing = write(tmp_path, "missing.csv", "id,prompt\nC-1,hi\n")
    with pytest.raises(ConfigError, match=r"missing\.csv.*missing column.*category"):
        load_pack(missing)
    extra = write(tmp_path, "extra.csv", "id,category,prompt,colour\nC-1,LLM01,hi,red\n")
    with pytest.raises(ConfigError, match=r"extra\.csv.*unknown column.*colour"):
        load_pack(extra)


def test_csv_skips_blank_rows_and_accepts_short_rows(tmp_path):
    text = f"{CSV_HEADER}\nC-1,LLM01,hi\n,,,,,,,\nC-2,LLM02,there,,,,,\n"
    assert [p.id for p in load_pack(write(tmp_path, "p.csv", text)).prompts] == ["C-1", "C-2"]


def test_csv_extra_cells_are_an_error_not_a_silently_shortened_prompt(tmp_path):
    path = write(tmp_path, "p.csv", "id,category,prompt\nC-3,LLM01,Say hi, then bye\n")
    with pytest.raises(ConfigError, match=r"p\.csv.*'C-3'.*more cells.*double quotes"):
        load_pack(path)


def test_csv_problem_names_file_and_prompt_id(tmp_path):
    path = write(tmp_path, "p.csv", f"{CSV_HEADER}\nC-9,LLM01,hi,lots,,,,\n")
    with pytest.raises(ConfigError, match=r"p\.csv.*'C-9'.*repeat"):
        load_pack(path)


def test_duplicate_ids_across_files_name_both(tmp_path):
    pack_dir = tmp_path / "p"
    pack_dir.mkdir()
    write_prompts(pack_dir, "a.yaml", item("X-1"))
    write_prompts(pack_dir, "b.yaml", item("X-1"))
    with pytest.raises(ConfigError) as caught:
        load_pack(pack_dir)
    message = str(caught.value)
    assert "X-1" in message and "a.yaml" in message and "b.yaml" in message


def test_duplicate_id_within_one_file_is_rejected(tmp_path):
    path = write_prompts(tmp_path, "a.yaml", item("X-1"), item("X-1"))
    with pytest.raises(ConfigError, match=r"X-1.*twice.*a\.yaml"):
        load_pack(path)


# --- several packs merged ----------------------------------------------------------------------


def make_pack_dir(tmp_path: Path, name: str, *items: dict) -> Path:
    pack_dir = tmp_path / name
    pack_dir.mkdir()
    write_prompts(pack_dir, "prompts.yaml", *items)
    return pack_dir


def test_load_packs_merges_in_order(tmp_path):
    first = make_pack_dir(tmp_path, "first", item("A2"), item("A1"))
    second = make_pack_dir(tmp_path, "second", item("B1"))
    third = write_prompts(tmp_path, "third.yaml", item("C1"), item("C2"))
    pack = load_packs([first, second, third])
    assert [p.id for p in pack.prompts] == ["A2", "A1", "B1", "C1", "C2"]
    assert pack.name == f"{first}+{second}+{third}"
    # Argument order decides the prompt order, not the path names.
    assert [p.id for p in load_packs([second, first]).prompts] == ["B1", "A2", "A1"]


def test_load_packs_with_one_path_is_load_pack(tmp_path):
    folder = make_pack_dir(tmp_path, "only", item("A1"), item("A2", repeat=3))
    assert load_packs([folder]) == load_pack(folder)
    assert load_packs([folder]).name == "only"


def test_duplicate_id_across_packs_is_rejected(tmp_path):
    a = make_pack_dir(tmp_path, "a", item("OK-1"), item("DUP-1"))
    b = make_pack_dir(tmp_path, "b", item("OK-2"), item("DUP-1"))
    with pytest.raises(ConfigError, match="DUP-1") as caught:
        load_packs([a, b])
    message = str(caught.value)
    assert str(a) in message and str(b) in message


def test_the_same_pack_given_twice_is_a_duplicate_not_a_silent_double_run(tmp_path):
    a = make_pack_dir(tmp_path, "a", item("X-1"))
    with pytest.raises(ConfigError, match="X-1.*more than once"):
        load_packs([a, a])


def test_load_packs_still_reports_a_problem_inside_one_pack(tmp_path):
    good = make_pack_dir(tmp_path, "good", item("G-1"))
    bad = make_pack_dir(tmp_path, "bad", item("B-1", category="LLM11"))
    with pytest.raises(ConfigError, match=r"bad.*'B-1'.*LLM11"):
        load_packs([good, bad])
    within = make_pack_dir(tmp_path, "within", item("W-1"), item("W-1"))
    with pytest.raises(ConfigError, match=r"W-1.*twice"):
        load_packs([good, within])


def test_load_packs_needs_at_least_one_path():
    with pytest.raises(ConfigError, match="no prompt pack"):
        load_packs([])


@pytest.mark.parametrize(
    "field,value",
    [
        ("category", "LLM11"),
        ("detectors", ["nope"]),
        ("must_not_match", ["nope"]),
        ("must_not_match", ["re:("]),
        ("must_not_contain", ["no"]),
        ("id", "A#1"),
    ],
)
def test_invalid_prompt_rejected_naming_id(tmp_path, field, value):
    fields = {"id": "BAD-1", "category": "LLM01", "prompt": "hello"}
    if field in ("id", "category"):
        fields[field] = value
    else:
        fields["checks"] = {field: value}
    path = write_prompts(tmp_path, "bad.yaml", fields)
    with pytest.raises(ConfigError) as caught:
        load_pack(path)
    message = str(caught.value)
    assert "bad.yaml" in message
    assert fields["id"] in message


def test_problems_are_readable(tmp_path):
    def message_for(**fields) -> str:
        path = write_prompts(tmp_path, "bad.yaml", item("BAD-1", **fields))
        with pytest.raises(ConfigError) as caught:
            load_pack(path)
        return str(caught.value)

    assert "LLM11" in message_for(category="LLM11")
    assert "LLM01" in message_for(category="LLM11")  # lists the valid categories
    assert "at least 6 ASCII letters or digits (A-Z, 0-9) — entries are matched loosely" in (
        message_for(checks={"must_not_contain": ["no"]})
    )
    assert "credential_leak" in message_for(checks={"detectors": ["nope"]})
    assert "invalid regular expression" in message_for(checks={"must_not_match": ["re:("]})
    assert "email_address" in message_for(checks={"must_not_match": ["nope"]})


def test_must_not_contain_counts_letters_and_digits_not_length(tmp_path):
    ok = write_prompts(
        tmp_path, "ok.yaml", item("OK-1", checks={"must_not_contain": ["SYSTEM-CANARY-7Q4P"]})
    )
    assert load_pack(ok).prompts[0].checks.must_not_contain == ["SYSTEM-CANARY-7Q4P"]
    padded = write_prompts(
        tmp_path, "padded.yaml", item("PAD-1", checks={"must_not_contain": ["ab-cd-e!!!!!!"]})
    )
    with pytest.raises(ConfigError, match="at least 6 ASCII letters or digits"):
        load_pack(padded)


@pytest.mark.parametrize("entry", ["пароль", "пароль1", "密码密码密码密码", "café12"])
def test_must_not_contain_counts_only_what_the_judge_can_match(tmp_path, entry):
    # The judge matches on A-Z and 0-9 after NFKC folding. An entry with fewer than 6 of those
    # (another script; an accented letter drops out) could never be checked, so it is refused
    # when the pack loads, not found out as a check error in the middle of a run.
    path = write_prompts(tmp_path, "nl.yaml", item("NL-1", checks={"must_not_contain": [entry]}))
    with pytest.raises(ConfigError, match=r"'NL-1'.*at least 6 ASCII letters or digits"):
        load_pack(path)


def test_must_not_contain_counts_look_alikes_the_judge_folds(tmp_path):
    full_width = "ＣＡＮＡＲＹ７"  # NFKC folds it to CANARY7, which the judge matches
    path = write_prompts(
        tmp_path, "fw.yaml", item("FW-1", checks={"must_not_contain": [full_width]})
    )
    assert load_pack(path).prompts[0].checks.must_not_contain == [full_width]


@pytest.mark.parametrize("repeat", [0, 21, -1])
def test_repeat_must_be_between_1_and_20(tmp_path, repeat):
    path = write_prompts(tmp_path, "r.yaml", item("R-1", repeat=repeat))
    with pytest.raises(ConfigError, match=r"r\.yaml.*'R-1'.*repeat"):
        load_pack(path)


def test_unknown_field_is_rejected(tmp_path):
    path = write_prompts(tmp_path, "u.yaml", item("U-1", severity="high"))
    with pytest.raises(ConfigError, match=r"u\.yaml.*'U-1'.*severity"):
        load_pack(path)


def test_numeric_id_is_reported_by_its_value(tmp_path):
    path = write(tmp_path, "n.yaml", "- id: 7\n  category: LLM01\n  prompt: hello\n")
    with pytest.raises(ConfigError, match=r"n\.yaml.*'7'"):
        load_pack(path)


def test_prompt_text_with_trailing_newline_survives_exactly(tmp_path):
    original = 'Line one "quoted"\nline two\n'
    path = write(tmp_path, "v.yaml", yaml.safe_dump([item("V-1", prompt=original)]))
    assert load_pack(path).prompts[0].prompt == original
    block = write(
        tmp_path,
        "block.yaml",
        '- id: B-1\n  category: LLM01\n  prompt: |\n    Line one "quoted"\n    line two\n',
    )
    assert load_pack(block).prompts[0].prompt == original


def test_prompt_text_is_verbatim_from_csv(tmp_path):
    original = '  keep "all" of this\r\nincluding a second line\n  '
    cell = '"' + original.replace('"', '""') + '"'
    path = write(tmp_path, "v.csv", f"id,category,prompt\nV-1,LLM01,{cell}\n")
    assert load_pack(path).prompts[0].prompt == original


def test_prompt_model_never_strips_or_normalises():
    padded = "  é́ \r\n tail \n"
    assert make_prompt(text=padded).prompt == padded
    assert Prompt(id="P-1", category="LLM01", prompt=padded).prompt == padded


def test_empty_prompt_text_is_rejected(tmp_path):
    path = write_prompts(tmp_path, "e.yaml", item("E-1", prompt=""))
    with pytest.raises(ConfigError, match=r"e\.yaml.*'E-1'.*prompt"):
        load_pack(path)


def test_empty_pack_rejected(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(ConfigError, match=r"empty.*no prompt files"):
        load_pack(empty_dir)
    with pytest.raises(ConfigError, match=r"none\.yaml.*no prompts"):
        load_pack(write(tmp_path, "none.yaml", "[]\n"))
    with pytest.raises(ConfigError, match=r"header\.csv.*no prompts"):
        load_pack(write(tmp_path, "header.csv", CSV_HEADER + "\n"))


def test_file_level_problems_name_the_file(tmp_path):
    with pytest.raises(ConfigError, match=r"nope\.yaml.*not found"):
        load_pack(tmp_path / "nope.yaml")
    with pytest.raises(ConfigError, match=r"pack\.txt.*\.yaml.*\.csv"):
        load_pack(write(tmp_path, "pack.txt", "x"))
    with pytest.raises(ConfigError, match=r"map\.yaml.*list of prompts"):
        load_pack(write(tmp_path, "map.yaml", "id: A\n"))
    with pytest.raises(ConfigError, match=r"blank\.yaml.*list of prompts"):
        load_pack(write(tmp_path, "blank.yaml", ""))
    with pytest.raises(ConfigError, match=r"broken\.yaml.*invalid YAML.*line"):
        load_pack(write(tmp_path, "broken.yaml", "- id: [unclosed\n"))
    with pytest.raises(ConfigError, match=r"scalar\.yaml.*item 2.*mapping"):
        load_pack(
            write(tmp_path, "scalar.yaml", "- id: A\n  category: LLM01\n  prompt: x\n- oops\n")
        )
    with pytest.raises(ConfigError, match=r"latin\.csv.*cannot read"):
        load_pack(write(tmp_path, "latin.csv", "id,category,prompt\nA,LLM01,caf\xe9\n", "latin-1"))


def test_checks_is_empty_only_when_nothing_is_configured():
    assert Checks().is_empty()
    assert not Checks(must_not_contain=["SYSTEM-CANARY-7Q4P"]).is_empty()
    assert not Checks(must_not_match=["email_address"]).is_empty()
    assert not Checks(detectors=["xss_output"]).is_empty()


def test_factories_build_valid_models():
    prompt = make_prompt(must_not_contain=["SYSTEM-CANARY-7Q4P"], detectors=["xss_output"])
    assert (prompt.id, prompt.category, prompt.prompt, prompt.repeat) == (
        "P-1",
        "LLM01",
        "hello",
        1,
    )
    assert prompt.checks.detectors == ["xss_output"]
    pack = make_pack(prompt, make_prompt(id="P-2", repeat=2))
    assert pack.name == "t" and [p.id for p in pack.prompts] == ["P-1", "P-2"]
