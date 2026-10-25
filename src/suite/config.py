"""Endpoint config: one YAML file describes one chatbot; this module loads and validates it.

Secrets never live in the YAML. A value may contain ``${VAR}``, which is replaced from the
environment (or a ``.env`` file in the working directory) before validation. Error messages
name the variable, never its value, and ``masked()`` hides every substituted value.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from dotenv import dotenv_values
from jsonpath_ng import parse as parse_jsonpath
from jsonpath_ng.exceptions import JSONPathError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from suite.models import ConfigError

GANDALF_LEVELS: tuple[str, ...] = (
    "baseline",
    "do-not-tell",
    "do-not-tell-and-block",
    "gpt-is-password-encoded",
    "word-blacklist",
    "gpt-blacklist",
    "gandalf",
    "gandalf-the-white",
    "adventure-1",
    "adventure-2",
)

PROMPT_PLACEHOLDER = "{prompt}"
# PyRIT's own placeholder in the raw request the HTTP adapter builds. A url or header holding it
# would receive the prompt, so config rejects it there; the prompt belongs in the body only.
PROMPT_MARKER = "{PROMPT}"
_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)\}")
# PyRIT strips a header name and splits the line at the first colon, so only plain tokens
# (RFC 9110) are safe: anything else could smuggle in a Host override or a second header.
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_DERIVED_HEADERS = frozenset({"host", "content-length"})  # taken from the url and the body


class Limits(BaseModel):
    """Per-endpoint safety limits the engine enforces."""

    model_config = ConfigDict(extra="forbid")

    concurrency: int = Field(default=10, ge=1, le=200)
    requests_per_minute: int = Field(default=120, ge=1)
    timeout_s: float = Field(default=60, gt=0)
    max_requests: int = Field(default=1000, ge=1)
    max_retries: int = Field(default=2, ge=0, le=5)
    abort_after_errors: int = Field(default=10, ge=1)


class EndpointSpec(BaseModel):
    """A validated description of one chatbot endpoint."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,47}$")
    type: Literal["http", "gandalf"]
    authorized: str | None = None
    url: str | None = None
    method: Literal["POST", "PUT"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] | None = None
    reply: str = "$.reply"
    level: str | None = None
    redact: bool = True
    limits: Limits = Field(default_factory=Limits)

    # Non-empty values that load_endpoint substituted for ${VAR}; masked() hides them.
    _secret_values: frozenset[str] = PrivateAttr(default_factory=frozenset)

    @field_validator("url")
    @classmethod
    def _url_is_http(cls, value: str | None) -> str | None:
        if value is not None:
            parts = urlsplit(value)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                raise ValueError("url must be an absolute http:// or https:// address")
            if PROMPT_MARKER in value:
                raise ValueError(
                    f"url must not contain {PROMPT_MARKER}; the prompt goes in the body"
                )
        return value

    @field_validator("headers")
    @classmethod
    def _headers_are_safe(cls, value: dict[str, str]) -> dict[str, str]:
        # Messages name a header only once its name is known to be a plain token, and never
        # quote a value: a value may be a secret substituted from ${VAR}.
        for name, text in value.items():
            if not _HEADER_NAME.fullmatch(name):
                raise ValueError(
                    "a header name may contain only letters, digits and these symbols: "
                    "!#$%&'*+-.^_`|~"
                )
            if name.lower() in _DERIVED_HEADERS:
                raise ValueError(f"the {name} header cannot be set; it comes from the url and body")
            if "\r" in text or "\n" in text:
                raise ValueError(f"header {name!r}: the value must not contain CR or LF")
            if PROMPT_MARKER in text:
                raise ValueError(
                    f"header {name!r}: the value must not contain {PROMPT_MARKER}; "
                    "the prompt goes in the body"
                )
        return value

    @field_validator("reply")
    @classmethod
    def _reply_is_jsonpath(cls, value: str) -> str:
        try:
            parse_jsonpath(value)
        except JSONPathError:
            raise ValueError("reply is not a valid JSONPath expression") from None
        return value

    @model_validator(mode="after")
    def _check_type_requirements(self) -> "EndpointSpec":
        if self.type == "http":
            if self.url is None:
                raise ValueError("type 'http' requires a url")
            if self.body is None:
                raise ValueError(f"type 'http' requires a body containing {PROMPT_PLACEHOLDER}")
            _check_single_placeholder(self.body)
        elif self.level not in GANDALF_LEVELS:
            raise ValueError(
                "type 'gandalf' requires a level, one of: " + ", ".join(GANDALF_LEVELS)
            )
        return self


def _strings(value: Any) -> list[str]:
    """Every string nested anywhere inside a parsed body (dict values and list items)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


def _check_single_placeholder(body: dict[str, Any]) -> None:
    holders = [s for s in _strings(body) if PROMPT_PLACEHOLDER in s]
    if not holders:
        raise ValueError(f"body must contain {PROMPT_PLACEHOLDER} in exactly one string value")
    if len(holders) > 1:
        raise ValueError(
            f"body must contain {PROMPT_PLACEHOLDER} in exactly one string value "
            f"(found it in {len(holders)})"
        )
    if holders[0].count(PROMPT_PLACEHOLDER) > 1:
        raise ValueError(f"{PROMPT_PLACEHOLDER} may appear only once in the body value")


@dataclass
class _Interpolation:
    """One pass of ``${VAR}`` substitution over a parsed YAML document."""

    env: Mapping[str, str]
    missing: list[str] = field(default_factory=list)  # variable names that are not set
    malformed: list[str] = field(default_factory=list)  # field paths holding a bad ``${...}``
    used: set[str] = field(default_factory=set)  # non-empty values that were substituted

    def walk(self, value: Any, where: str = "") -> Any:
        """Substitute in every string; ``where`` is the field path, for error messages."""
        if isinstance(value, str):
            return self._string(value, where)
        if isinstance(value, dict):
            return {
                key: self.walk(item, f"{where}.{key}" if where else str(key))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.walk(item, f"{where}[{i}]") for i, item in enumerate(value)]
        return value

    def _string(self, text: str, where: str) -> str:
        def substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in self.env:
                if name not in self.missing:
                    self.missing.append(name)
                return match.group(0)
            if self.env[name]:
                self.used.add(self.env[name])
            return self.env[name]

        # A "${" that is not a well-formed reference (e.g. ${token}) would otherwise be sent
        # literally. Check the template, not the result: a secret may legitimately contain "${".
        if "${" in _ENV_REF.sub("", text):
            self.malformed.append(where)
        return _ENV_REF.sub(substitute, text)


def _default_env() -> dict[str, str]:
    file_values = {k: v for k, v in dotenv_values(Path.cwd() / ".env").items() if v is not None}
    return {**file_values, **os.environ}


def _describe(error: ValidationError) -> str:
    """Readable problems from a ValidationError, without echoing any input value."""
    problems = []
    for item in error.errors(include_input=False, include_url=False):
        where = ".".join(str(part) for part in item["loc"])
        message = item["msg"].removeprefix("Value error, ")
        problems.append(f"{where}: {message}" if where else message)
    return "; ".join(problems)


def load_endpoint(path: Path, *, env: Mapping[str, str] | None = None) -> EndpointSpec:
    """Load, interpolate and validate one endpoint YAML file.

    Every failure is raised as a ConfigError whose message starts with the file path.
    """
    if env is None:
        env = _default_env()

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: cannot read file ({exc.__class__.__name__})") from None

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        problem = getattr(exc, "problem", None) or "invalid YAML"
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ConfigError(f"{path}: invalid YAML{where}: {problem}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a YAML mapping of endpoint settings")

    interpolation = _Interpolation(env)
    data = interpolation.walk(raw)
    if interpolation.malformed:
        raise ConfigError(
            f"{path}: invalid variable reference in {', '.join(interpolation.malformed)}: "
            "variable names must be UPPER_CASE letters, digits and underscores, like ${MY_TOKEN}"
        )
    if interpolation.missing:
        names = ", ".join("${" + name + "}" for name in interpolation.missing)
        raise ConfigError(
            f"{path}: environment variable not set: {names} (define it in .env or the environment)"
        )

    try:
        spec = EndpointSpec.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path}: {_describe(exc)}") from None
    spec._secret_values = frozenset(interpolation.used)
    return spec


def _scrub(value: Any, secrets: list[str]) -> Any:
    """Copy of ``value`` with every secret substring in any string replaced by ``***``."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "***")
        return value
    if isinstance(value, dict):
        return {key: _scrub(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, secrets) for item in value]
    return value


def masked(spec: EndpointSpec) -> dict[str, Any]:
    """The spec as a plain dict that is safe to store or print; the spec is not changed.

    Every header value becomes ``***``. In every other string (url, body values, nested lists
    and dicts, any field) each non-empty value that was substituted for a ``${VAR}`` becomes
    ``***``. A secret typed literally into the YAML cannot be recognised, so keep secrets in
    ``${VAR}`` references.
    """
    # Longest first, so a secret that contains another is not left half-visible.
    secrets = sorted(spec._secret_values, key=len, reverse=True)
    dumped: dict[str, Any] = _scrub(spec.model_dump(), secrets)
    dumped["headers"] = {name: "***" for name in dumped["headers"]}
    return dumped
