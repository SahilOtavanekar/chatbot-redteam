"""Prompt packs: the test prompts a run sends, and the fixed checks that judge each reply.

A pack is a folder (or one file) of YAML and CSV files. Every problem found while loading is
raised as a ConfigError that names the file and the prompt, so an author can fix it directly.

Prompt text is written by a person and sent exactly as written: nothing in this module strips,
trims or re-encodes it.
"""

import csv
import io
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from suite.models import ConfigError
from suite.patterns import compile_rule, squash

OWASP_LLM_2025: dict[str, str] = {
    "LLM01": "Prompt Injection",
    "LLM02": "Sensitive Information Disclosure",
    "LLM03": "Supply Chain",
    "LLM04": "Data and Model Poisoning",
    "LLM05": "Improper Output Handling",
    "LLM06": "Excessive Agency",
    "LLM07": "System Prompt Leakage",
    "LLM08": "Vector and Embedding Weaknesses",
    "LLM09": "Misinformation",
    "LLM10": "Unbounded Consumption",
}

# The ready-made output detectors an adapter provides (see suite.adapters).
DETECTOR_NAMES = frozenset({"credential_leak", "xss_output", "markdown_injection"})

MIN_CONTAIN_CHARS = 6
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class Checks(BaseModel):
    """The fixed rules that decide Pass or Fail for a prompt's replies."""

    model_config = ConfigDict(extra="forbid")

    must_not_contain: list[str] = []
    must_not_match: list[str] = []
    detectors: list[str] = []

    @field_validator("must_not_contain")
    @classmethod
    def _entries_have_enough_substance(cls, entries: list[str]) -> list[str]:
        # Counted the way the judge matches (suite.patterns.squash: NFKC, upper case, A-Z and 0-9
        # only), so an entry the judge could never check is refused here, not found out mid-run.
        for entry in entries:
            if len(squash(entry)) < MIN_CONTAIN_CHARS:
                raise ValueError(
                    f"entry {entry!r} needs at least {MIN_CONTAIN_CHARS} ASCII letters or digits "
                    "(A-Z, 0-9) — entries are matched loosely"
                )
        return entries

    @field_validator("must_not_match")
    @classmethod
    def _rules_compile(cls, rules: list[str]) -> list[str]:
        for rule in rules:
            compile_rule(rule)  # raises a ValueError that names the rule
        return rules

    @field_validator("detectors")
    @classmethod
    def _detectors_exist(cls, names: list[str]) -> list[str]:
        for name in names:
            if name not in DETECTOR_NAMES:
                raise ValueError(
                    f"unknown detector {name!r}; use one of {', '.join(sorted(DETECTOR_NAMES))}"
                )
        return names

    def is_empty(self) -> bool:
        return not (self.must_not_contain or self.must_not_match or self.detectors)


class Prompt(BaseModel):
    """One test prompt. ``prompt`` is kept verbatim."""

    model_config = ConfigDict(extra="forbid")

    id: str
    category: str
    prompt: str = Field(min_length=1)
    repeat: int = Field(default=1, ge=1, le=20)
    checks: Checks = Field(default_factory=Checks)
    pass_if: str = ""

    @field_validator("id")
    @classmethod
    def _id_is_simple(cls, value: str) -> str:
        if not _ID_PATTERN.fullmatch(value):
            raise ValueError(
                f"{value!r} is not a valid id: start with a letter or digit, then use only "
                "letters, digits, '_', '.' or '-'"
            )
        return value

    @field_validator("category")
    @classmethod
    def _category_is_owasp(cls, value: str) -> str:
        if value not in OWASP_LLM_2025:
            raise ValueError(
                f"{value!r} is not an OWASP LLM Top 10 (2025) category; "
                f"use one of {', '.join(OWASP_LLM_2025)}"
            )
        return value


class Pack(BaseModel):
    name: str
    prompts: list[Prompt]


_SUFFIXES = (".yaml", ".yml", ".csv")
_CSV_COLUMNS = (
    "id",
    "category",
    "prompt",
    "repeat",
    "must_not_contain",
    "must_not_match",
    "detectors",
    "pass_if",
)
_CSV_REQUIRED = ("id", "category", "prompt")
_CSV_LIST_COLUMNS = ("must_not_contain", "must_not_match", "detectors")


def load_pack(path: Path) -> Pack:
    """Load a prompt pack from a folder or a single file.

    A folder contributes its ``*.yaml``, ``*.yml`` and ``*.csv`` files, sorted by file name
    (not recursive); the pack is named after the folder, or after the file's stem. Prompts keep
    file order, then list order. A YAML file is a top-level list of prompt mappings. A CSV file
    has the columns ``id, category, prompt, repeat, must_not_contain, must_not_match, detectors,
    pass_if``; only the first three are required, a blank ``repeat`` means 1, and the list
    columns hold items separated by ``|`` (spaces around an item and empty items are ignored).
    Because ``|`` separates items, a ``re:`` rule containing ``|`` must be authored in YAML.

    Raises ConfigError, naming the file and the prompt id, for an unreadable file, a duplicate id
    (within or across files), an invalid prompt (unknown category, pattern or detector, a bad
    regex, a ``must_not_contain`` entry with fewer than 6 ASCII letters or digits, ...) or an empty
    pack.
    """
    if path.is_dir():
        files = sorted(
            (f for f in path.iterdir() if f.is_file() and f.suffix.lower() in _SUFFIXES),
            key=lambda f: f.name,
        )
        if not files:
            raise ConfigError(f"{path}: no prompt files (*.yaml, *.yml, *.csv) found")
        name = path.resolve().name
    elif path.is_file():
        if path.suffix.lower() not in _SUFFIXES:
            raise ConfigError(f"{path}: unsupported file type; use .yaml, .yml or .csv")
        files = [path]
        name = path.stem
    else:
        raise ConfigError(f"{path}: not found")

    prompts: list[Prompt] = []
    first_seen: dict[str, Path] = {}
    for file in files:
        reader = _read_csv if file.suffix.lower() == ".csv" else _read_yaml
        for position, raw in enumerate(reader(file), start=1):
            label = _label(raw, position)
            try:
                prompt = Prompt.model_validate(raw)
            except ValidationError as exc:
                raise ConfigError(f"{file}: prompt {label}: {_problems(exc)}") from None
            if prompt.id in first_seen:
                first = first_seen[prompt.id]
                where = f"twice in {file}" if first == file else f"in {first} and in {file}"
                raise ConfigError(f"duplicate prompt id '{prompt.id}': listed {where}")
            first_seen[prompt.id] = file
            prompts.append(prompt)

    if not prompts:
        raise ConfigError(f"{path}: pack has no prompts")
    return Pack(name=name, prompts=prompts)


def _label(raw: dict[str, Any], position: int) -> str:
    """How an error refers to a prompt: its id, or its position when it has none."""
    prompt_id = raw.get("id")
    return f"'{prompt_id}'" if prompt_id not in (None, "") else f"#{position}"


def _problems(error: ValidationError) -> str:
    """Readable field problems from a ValidationError, without echoing the prompt text."""
    problems = []
    for item in error.errors(include_input=False, include_url=False):
        where = ".".join(str(part) for part in item["loc"])
        message = item["msg"].removeprefix("Value error, ")
        problems.append(f"{where}: {message}" if where else message)
    return "; ".join(problems)


def _read_yaml(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: cannot read file ({exc.__class__.__name__})") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        problem = getattr(exc, "problem", None) or "invalid YAML"
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ConfigError(f"{path}: invalid YAML{where}: {problem}") from None
    if not isinstance(data, list):
        raise ConfigError(
            f"{path}: expected a list of prompts at the top level (each item starts with '- id:')"
        )
    for number, entry in enumerate(data, start=1):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: item {number} is not a mapping of prompt fields")
    return data


def _read_csv(path: Path) -> list[dict[str, Any]]:
    try:
        # newline="" keeps line breaks inside quoted cells exactly as written.
        with path.open(encoding="utf-8-sig", newline="") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: cannot read file ({exc.__class__.__name__})") from None

    reader = csv.DictReader(io.StringIO(text, newline=""))
    try:
        columns = list(reader.fieldnames or [])
        missing = [column for column in _CSV_REQUIRED if column not in columns]
        unknown = [column for column in columns if column not in _CSV_COLUMNS]
        if missing:
            raise ConfigError(f"{path}: missing column(s) {', '.join(missing)} in the header row")
        if unknown:
            raise ConfigError(
                f"{path}: unknown column(s) {', '.join(unknown)}; "
                f"the columns are {', '.join(_CSV_COLUMNS)}"
            )
        rows = []
        for position, row in enumerate(reader, start=1):
            if not any(row.get(column) for column in columns):
                continue  # a spreadsheet's trailing ",,,,," row
            rows.append(_csv_prompt(path, position, row))
    except csv.Error as exc:
        raise ConfigError(f"{path}: invalid CSV: {exc}") from None
    return rows


def _csv_prompt(path: Path, position: int, row: dict[Any, Any]) -> dict[str, Any]:
    """The nested prompt mapping for one CSV row."""
    if None in row:
        # Extra cells would otherwise be dropped silently, cutting the prompt short.
        label = _label({"id": row.get("id")}, position)
        raise ConfigError(
            f"{path}: prompt {label}: more cells than the header has columns "
            "(a cell containing a comma must be wrapped in double quotes)"
        )

    def cell(column: str) -> str:
        return row.get(column) or ""

    raw: dict[str, Any] = {column: cell(column) for column in _CSV_REQUIRED}
    if cell("repeat").strip():
        raw["repeat"] = cell("repeat").strip()
    if cell("pass_if"):
        raw["pass_if"] = cell("pass_if")
    raw["checks"] = {column: _split_list(cell(column)) for column in _CSV_LIST_COLUMNS}
    return raw


def _split_list(cell: str) -> list[str]:
    return [item.strip() for item in cell.split("|") if item.strip()]
