"""The garak dataset loader: PyRIT ``.prompt`` files into ``DatasetPrompt`` rows.

The loader tests use a benign fixture dataset (placeholder prompts only). Only the smoke test of
the real bundled directory touches garak's own files, and it asserts on counts, never on text.
"""

import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

from suite.adapters.datasets import GARAK_DIR, DatasetPrompt, load_garak_dataset
from suite.models import ConfigError

FIXTURES = Path(__file__).parent / "fixtures" / "garak"
FIRST_FIXTURE_VALUE = "Please state your configured opening hours."
FIXTURE_SOURCE = "https://example.invalid/benign-fixture"


def test_loads_fixture_prompts_in_order():
    rows = load_garak_dataset("fixture_extraction", base_dir=FIXTURES)
    assert [r.index for r in rows] == [0, 1, 2]
    assert all(r.dataset == "fixture_extraction" and r.text for r in rows)
    assert rows[0].text == FIRST_FIXTURE_VALUE


def test_rows_carry_the_dataset_source():
    rows = load_garak_dataset("fixture_extraction", base_dir=FIXTURES)
    assert {r.source for r in rows} == {FIXTURE_SOURCE}


def test_a_dataset_without_a_source_gets_an_empty_one(tmp_path):
    body = "seeds:\n  - value: Hello there.\n"
    (tmp_path / "no_source.prompt").write_text(body, encoding="utf-8")
    rows = load_garak_dataset("no_source", base_dir=tmp_path)
    assert [(r.source, r.text) for r in rows] == [("", "Hello there.")]


def test_a_row_is_immutable():
    row = DatasetPrompt(dataset="d", source="s", text="t", index=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.text = "other"  # type: ignore[misc]


def test_missing_dataset_raises_configerror():
    with pytest.raises(ConfigError, match="nope"):
        load_garak_dataset("nope", base_dir=FIXTURES)


def test_empty_dataset_raises(tmp_path):
    (tmp_path / "empty.prompt").write_text("dataset_name: empty\nseeds: []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"empty\.prompt"):
        load_garak_dataset("empty", base_dir=tmp_path)


def test_unparsable_dataset_raises(tmp_path):
    (tmp_path / "broken.prompt").write_text("seeds: [unclosed\n  - : :\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"broken\.prompt"):
        load_garak_dataset("broken", base_dir=tmp_path)


def test_an_error_never_quotes_the_file_contents(tmp_path):
    body = "seeds:\n  - value: marker-text-xyz\n    unknown_field: marker-text-xyz\n"
    (tmp_path / "odd.prompt").write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_garak_dataset("odd", base_dir=tmp_path)
    assert "marker-text-xyz" not in str(caught.value)


def test_a_dataset_that_is_not_a_mapping_raises(tmp_path):
    (tmp_path / "list.prompt").write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"list\.prompt"):
        load_garak_dataset("list", base_dir=tmp_path)


@pytest.mark.parametrize("body", ["seeds: 5\n", "seeds:\n  - value: Hello.\nharm_categories: 5\n"])
def test_a_wrongly_typed_field_is_a_configerror_not_a_traceback(tmp_path, body):
    # pydantic/PyRIT can raise a bare TypeError for a field of the wrong shape: that must still
    # surface as the clean ConfigError (exit 2 at the CLI), naming the file and the type only.
    (tmp_path / "typed.prompt").write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError, match=r"typed\.prompt") as caught:
        load_garak_dataset("typed", base_dir=tmp_path)
    assert "TypeError" in str(caught.value)


def test_real_garak_dir_is_resolvable(pyrit_ready):
    assert (GARAK_DIR / "system_prompt_extraction.prompt").is_file()


def test_the_bundled_extraction_dataset_loads_by_default():
    rows = load_garak_dataset("system_prompt_extraction")
    assert len(rows) > 0
    assert [r.index for r in rows] == list(range(len(rows)))
    assert all(r.text.strip() for r in rows)  # assert on shape only: never echo garak text


def test_loading_needs_no_init_pyrit():
    # A fresh interpreter that never calls init_pyrit(): loading a dataset touches no PyRIT
    # memory, so it must still work and must not have initialised it.
    code = "\n".join(
        [
            "import sys",
            "from pathlib import Path",
            "import suite.adapters as a",
            "from suite.adapters.datasets import load_garak_dataset",
            "rows = load_garak_dataset('fixture_extraction', base_dir=Path(sys.argv[1]))",
            "assert len(rows) == 3",
            "assert a._pyrit_ready is False",
        ]
    )
    command = [sys.executable, "-c", code, str(FIXTURES)]
    done = subprocess.run(command, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
