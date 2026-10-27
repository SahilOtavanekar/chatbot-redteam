"""Shared fixtures: a session-wide mock bot and PyRIT's central memory."""

from collections.abc import Iterator

import pytest

from tests.mockbot import MockState, running_mockbot


@pytest.fixture(scope="session")
def _mockbot_server() -> Iterator[tuple[str, MockState]]:
    with running_mockbot() as server:
        yield server


@pytest.fixture
def mockbot(_mockbot_server: tuple[str, MockState]) -> tuple[str, MockState]:
    """``(base_url, state)`` of the running mock bot, with its state reset for this test."""
    url, state = _mockbot_server
    state.reset()
    return url, state


@pytest.fixture
async def pyrit_ready() -> None:
    """PyRIT targets need central memory; this initialises it once per process."""
    # Imported here, not at the top: importing suite.adapters loads PyRIT (about 6 s), and a
    # run that selects only tests without this fixture should not pay for it.
    from suite.adapters import init_pyrit

    await init_pyrit()
