"""Endpoint config: one YAML file describes one chatbot; this module loads and validates it.

Secrets never live in the YAML. A value may contain ``${VAR}``, which is replaced from the
environment (or a ``.env`` file in the working directory) before validation. Error messages
name the variable, never its value.
"""

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from dotenv import dotenv_values
from jsonpath_ng import parse as parse_jsonpath
from jsonpath_ng.exceptions import JSONPathError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

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
_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)\}")


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

    @field_validator("url")
    @classmethod
    def _url_is_http(cls, value: str | None) -> str | None:
        if value is not None:
            parts = urlsplit(value)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                raise ValueError("url must be an absolute http:// or https:// address")
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


def _interpolate(value: Any, env: Mapping[str, str], missing: list[str]) -> Any:
    """Replace ``${VAR}`` in every string; record unset variable names in ``missing``."""
    if isinstance(value, str):

        def substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in env:
                if name not in missing:
                    missing.append(name)
                return match.group(0)
            return env[name]

        return _ENV_REF.sub(substitute, value)
    if isinstance(value, dict):
        return {key: _interpolate(item, env, missing) for key, item in value.items()}
    if isinstance(value, list):
        return [_interpolate(item, env, missing) for item in value]
    return value


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

    missing: list[str] = []
    data = _interpolate(raw, env, missing)
    if missing:
        names = ", ".join("${" + name + "}" for name in missing)
        raise ConfigError(
            f"{path}: environment variable not set: {names} (define it in .env or the environment)"
        )

    try:
        return EndpointSpec.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path}: {_describe(exc)}") from None


def masked(spec: EndpointSpec) -> dict[str, Any]:
    """The spec as a plain dict that is safe to store or print: header values become ``***``."""
    dumped = spec.model_dump()
    dumped["headers"] = {name: "***" for name in dumped["headers"]}
    return dumped
