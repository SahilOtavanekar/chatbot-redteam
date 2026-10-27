"""Tests for the ``suite web`` command.

Executed: every test runs ``main`` or ``_port_free`` for real. ``uvicorn.run`` is replaced, so no
server starts. ``_port_free`` is patched in the command tests, so only the last test binds a
socket (an ephemeral loopback port it holds itself).
"""

import socket
from pathlib import Path
from typing import Any

import pytest

from suite.cli import EXIT_USER_ERROR, _port_free, main


def test_web_command_builds_settings_and_launches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr("suite.cli._port_free", lambda host, port: True)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: seen.update(kw))

    rc = main(["web", "--results-dir", str(tmp_path), "--port", "8790"])

    assert rc == 0
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == 8790


def test_web_command_errors_on_busy_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    launched: list[object] = []
    monkeypatch.setattr("suite.cli._port_free", lambda host, port: False)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: launched.append(app))

    rc = main(["web", "--results-dir", str(tmp_path), "--port", "8790"])

    assert rc == EXIT_USER_ERROR
    assert launched == []  # the port is checked before any server starts


def test_web_command_warns_when_not_loopback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("suite.cli._port_free", lambda host, port: True)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: None)

    assert main(["web", "--results-dir", str(tmp_path), "--host", "0.0.0.0"]) == 0
    assert "no login" in capsys.readouterr().err

    assert main(["web", "--results-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().err == ""  # the default loopback host stays quiet


def test_port_free_sees_a_held_port() -> None:
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        assert _port_free("127.0.0.1", port) is False
    assert _port_free("127.0.0.1", port) is True
