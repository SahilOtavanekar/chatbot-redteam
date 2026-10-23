"""Test helpers shared across the suite's tests."""

from typing import Any

from suite.config import EndpointSpec
from suite.prompts import Checks, Pack, Prompt


def make_spec(**overrides: Any) -> EndpointSpec:
    """Build a valid http EndpointSpec; keyword overrides replace the defaults.

    A ``limits=dict(...)`` override is merged into the default limits, so a test can set
    one limit without restating the others.
    """
    fields: dict[str, Any] = {
        "name": "t",
        "type": "http",
        "authorized": "test",
        "url": "http://127.0.0.1:9/chat",
        "body": {"message": "{prompt}"},
    }
    fields.update(overrides)
    return EndpointSpec(**fields)


def make_prompt(
    id: str = "P-1",
    category: str = "LLM01",
    text: str = "hello",
    repeat: int = 1,
    **checks: Any,
) -> Prompt:
    """Build a valid Prompt; extra keywords (``must_not_contain=[...]``, ...) become its checks."""
    return Prompt(id=id, category=category, prompt=text, repeat=repeat, checks=Checks(**checks))


def make_pack(*prompts: Prompt, name: str = "t") -> Pack:
    return Pack(name=name, prompts=list(prompts))
