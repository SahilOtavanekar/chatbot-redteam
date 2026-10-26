"""Turn a prompt dataset into baseline pack files and a coverage statement.

``import_source("garak", out_dir, canary=...)`` loads every dataset named in the committed
``GARAK_MAP`` (through ``suite.adapters.datasets``, so this module imports no PyRIT), builds one
``Prompt`` per dataset prompt, and writes one pack file per OWASP category plus ``COVERAGE.md``.
The result is an ordinary prompt pack: ``suite validate`` and ``suite run`` read it like any
other. Nothing here sends a prompt anywhere.

The text imported is what PyRIT's loader hands back: the seed's value after PyRIT's Jinja
rendering (a trailing newline stripped, ``{# ... #}`` comments removed), not the file's literal
bytes. That is deterministic, so the baseline is exactly the rendered garak prompts.

Output is deterministic (same inputs, byte-identical files: fixed YAML style, prompts in id
order, no timestamps) and written atomically (temp file in the folder, then ``os.replace``), so a
crash never leaves a half-written file. Every prompt is validated by the v0.1 ``Prompt`` model
before anything is written, and the written folder is read back with ``load_pack`` as a
self-check. The generated files are git-ignored: regenerate them, never edit them.
"""

import os
import tempfile
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from suite.adapters.datasets import GARAK_DIR, DatasetPrompt, load_garak_dataset
from suite.importer_map import COVERED, GARAK_MAP, MapRow
from suite.models import ConfigError
from suite.patterns import squash
from suite.prompts import (
    MIN_CONTAIN_CHARS,
    OWASP_LLM_2025,
    Checks,
    Prompt,
    _problems,
    load_pack,
)

SOURCE_GARAK = "garak"
COVERAGE_FILE = "COVERAGE.md"
_REGENERATE = "git-ignored — regenerate, don't edit."


@dataclass(frozen=True)
class ImportResult:
    """What an import wrote: the folder, every file in it, and the totals."""

    out_dir: Path
    files: list[Path]
    prompt_count: int
    categories: list[str]


@dataclass(frozen=True)
class _Section:
    """One output pack file: a category's prompts (in id order) and where they came from."""

    category: str
    prompts: list[Prompt]
    sources: dict[str, str]  # dataset -> its source URL ("" if it has none)

    @property
    def file_name(self) -> str:
        return f"{self.category}.yaml"


def build_prompts(
    rows: list[DatasetPrompt], row_map: MapRow, *, canary: str | None, repeat: int
) -> list[Prompt]:
    """One validated ``Prompt`` per dataset row, judged as ``row_map`` says.

    The id is ``BASE-<category>-<dataset>-<NNN>``, numbered from the row's file position. The
    detectors always apply; the canary becomes a ``must_not_contain`` entry only when the map row
    asks for it and a canary was given (so a prompt may end up with no canary check, never with
    a weaker one). A row the ``Prompt`` model refuses raises ``ConfigError`` naming the dataset
    and the row's index, never quoting the text.
    """
    prompts = []
    for row in rows:
        checks = Checks(
            detectors=list(row_map.detectors),
            must_not_contain=[canary] if (row_map.canary_check and canary) else [],
        )
        raw = {
            "id": f"BASE-{row_map.category}-{row_map.dataset}-{row.index + 1:03d}",
            "category": row_map.category,
            "prompt": row.text,
            "repeat": repeat,
            "checks": checks,
            "pass_if": row_map.default_pass_if,
        }
        try:
            prompts.append(Prompt.model_validate(raw))
        except ValidationError as exc:
            raise ConfigError(
                f"dataset {row_map.dataset!r}, prompt at index {row.index}: {_problems(exc)}"
            ) from None
    return prompts


def import_source(
    source: str,
    out_dir: Path,
    *,
    canary: str | None,
    repeat: int = 1,
    base_dir: Path = GARAK_DIR,
) -> ImportResult:
    """Write the baseline pack files and ``COVERAGE.md`` for ``source`` into ``out_dir``.

    ``source`` must be ``"garak"``. Every mapped dataset is loaded and every prompt validated
    before the first file is written, so a bad dataset, canary or repeat leaves ``out_dir``
    untouched. Raises ``ConfigError`` for an unknown source, a canary with fewer than
    ``MIN_CONTAIN_CHARS`` ASCII letters or digits, a missing or unreadable dataset, a prompt the
    pack model refuses, or (after writing) an output folder that does not load as a valid pack.
    """
    if source != SOURCE_GARAK:
        raise ConfigError(f"unknown source {source!r}; the only source is {SOURCE_GARAK!r}")
    _check_canary(canary)

    by_category: dict[str, list[Prompt]] = {}
    sources: dict[str, dict[str, str]] = {}  # category -> dataset -> its source URL
    for row_map in GARAK_MAP.values():
        _check_dataset_name(row_map.dataset)
        rows = load_garak_dataset(row_map.dataset, base_dir=base_dir)
        prompts = build_prompts(rows, row_map, canary=canary, repeat=repeat)
        by_category.setdefault(row_map.category, []).extend(prompts)
        sources.setdefault(row_map.category, {})[row_map.dataset] = rows[0].source
    if not by_category:
        raise ConfigError("no datasets are mapped for import; GARAK_MAP is empty")

    sections = [
        _Section(category, sorted(by_category[category], key=lambda p: p.id), sources[category])
        for category in sorted(by_category)
    ]
    texts = {section.file_name: _render_pack(section) for section in sections}
    texts[COVERAGE_FILE] = _render_coverage(sections)

    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for file_name, text in texts.items():
        path = out_dir / file_name
        _atomic_write_text(path, text)
        files.append(path)

    built = [prompt for section in sections for prompt in section.prompts]
    _self_check(out_dir, built)
    return ImportResult(
        out_dir=out_dir,
        files=files,
        prompt_count=len(built),
        categories=[section.category for section in sections],
    )


def _check_canary(canary: str | None) -> None:
    if canary is None:
        return
    # Counted the way the judge matches (squash), so a marker the judge could never find is
    # refused here, before any file is written.
    if len(squash(canary)) < MIN_CONTAIN_CHARS:
        raise ConfigError(
            f"--canary {canary!r} needs at least {MIN_CONTAIN_CHARS} ASCII letters or digits "
            "(A-Z, 0-9) — the marker is matched loosely"
        )


def _check_dataset_name(name: str) -> None:
    """A dataset name becomes a file name: it must not be able to point outside the folder."""
    if not name or ".." in name or "/" in name or "\\" in name or os.sep in name:
        raise ConfigError(
            f"invalid dataset name {name!r}: a dataset name must not contain a path separator "
            "or '..'"
        )


def _pyrit_version() -> str:
    try:
        return metadata.version("pyrit")
    except metadata.PackageNotFoundError:
        return "unknown"


def _one_line(text: str) -> str:
    """Collapse whitespace so a value can never break out of a ``#`` comment line."""
    return " ".join(text.split())


def _render_pack(section: _Section) -> str:
    header = [
        f"# Baseline prompt pack: {section.category} {OWASP_LLM_2025[section.category]}",
        "# Generated by `suite import --source garak`. Source: garak probe prompts, loaded "
        f"through PyRIT {_pyrit_version()} (Apache-2.0).",
    ]
    for dataset, url in section.sources.items():
        header.append(f"# Dataset: {dataset}" + (f" ({_one_line(url)})" if url else ""))
    header.append(f"# {_REGENERATE}")
    body = yaml.safe_dump(
        [_prompt_data(prompt) for prompt in section.prompts],
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=4096,
    )
    return "\n".join(header) + "\n\n" + body


def _prompt_data(prompt: Prompt) -> dict[str, Any]:
    """The prompt as a plain mapping, empty check lists left out."""
    checks = {
        name: values
        for name, values in (
            ("must_not_contain", prompt.checks.must_not_contain),
            ("must_not_match", prompt.checks.must_not_match),
            ("detectors", prompt.checks.detectors),
        )
        if values
    }
    data: dict[str, Any] = {
        "id": prompt.id,
        "category": prompt.category,
        "prompt": prompt.prompt,
        "repeat": prompt.repeat,
    }
    if checks:
        data["checks"] = checks
    if prompt.pass_if:
        data["pass_if"] = prompt.pass_if
    return data


def _render_coverage(sections: list[_Section]) -> str:
    lines = [
        "# Baseline coverage: OWASP Top 10 for LLM Applications (2025)",
        "",
        "Generated by `suite import --source garak` from garak's probe prompts, loaded through "
        f"PyRIT {_pyrit_version()} (Apache-2.0). {_REGENERATE}",
        "",
        "Replies are judged by fixed rules, never by a model; a prompt with no rule goes to "
        "human review, and an inconclusive reply is never counted as a Pass.",
        "",
        "## Imported by this run",
        "",
        "| File | Category | Datasets | Prompts |",
        "|---|---|---|---|",
    ]
    for section in sections:
        datasets = ", ".join(section.sources)
        lines.append(
            f"| {section.file_name} | {section.category} | {datasets} | {len(section.prompts)} |"
        )
    lines += [
        "",
        "## Coverage by category",
        "",
        "| Category | Name | Coverage |",
        "|---|---|---|",
    ]
    for code, name in OWASP_LLM_2025.items():
        note = COVERED[code].replace("|", "\\|")
        lines.append(f"| {code} | {name} | {note} |")
    return "\n".join(lines) + "\n"


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``path`` atomically: a crash mid-write never leaves a truncated or partial file."""
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _self_check(out_dir: Path, built: list[Prompt]) -> None:
    """The importer's own output must load as a pack and hold exactly what was built."""
    try:
        pack = load_pack(out_dir)
    except ConfigError as exc:
        raise ConfigError(f"the imported pack in {out_dir} does not load: {exc}") from None
    loaded = {prompt.id: prompt for prompt in pack.prompts}
    for prompt in built:
        if loaded.get(prompt.id) != prompt:
            raise ConfigError(
                f"the imported pack in {out_dir} does not match what was built "
                f"(prompt {prompt.id!r} changed in the YAML round trip)"
            )
