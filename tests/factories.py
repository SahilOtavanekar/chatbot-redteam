"""Test helpers shared across the suite's tests."""

from typing import Any

from suite.config import EndpointSpec


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
