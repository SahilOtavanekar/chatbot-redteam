import re
from pathlib import Path

import pytest

# An import of an engine module: ``from suite.x import ...`` or ``import suite.x`` for any x
# (models, config, engine, planner, report, ...), and the bare ``from suite import x``.
# ``suite.web`` is the web layer itself, not the engine.
ENGINE_IMPORT = re.compile(
    r"^\s*(?:(?:from|import)\s+suite\.(?!web\b)\w+|from\s+suite\s+import\b)", re.M
)


@pytest.mark.parametrize(
    "line",
    [
        "from suite.report import write_reports",
        "from suite.review import finalize",
        "from suite.compare import compare_runs",
        "from suite.evidence import read_run_json",
        "from suite.prompts import OWASP_LLM_2025",
        "from suite.models import Verdict",
        "from suite.config import load_endpoint",
        "from suite.engine import RateLimiter",
        "from suite.planner import preflight",
        "from suite.run import execute_run",
        "from suite.adapters import build_sender",
        "import suite.models",
        "import suite.engine as engine",
        "    from suite.config import masked  # a lazy import inside a function",
        "from suite import models",
        "from suite import (report, review)",
    ],
)
def test_the_guard_flags_an_engine_import(line):
    assert ENGINE_IMPORT.search(line + "\n")


@pytest.mark.parametrize(
    "line",
    [
        "from suite.web import service",
        "from suite.web.settings import WebSettings",
        "import suite.web.service",
        "from fastapi import FastAPI",
        "# from suite.models import Verdict  (a comment is not an import)",
        "x = 'from suite.models import Verdict'",
    ],
)
def test_the_guard_leaves_web_and_foreign_imports_alone(line):
    assert not ENGINE_IMPORT.search(line + "\n")


def test_only_service_imports_engine():
    """Within ``suite.web``, only ``service.py`` may import an engine module.

    The routers and views go through the service layer, so the engine stays behind one seam.
    """
    web = Path(__file__).parents[1] / "src" / "suite" / "web"
    assert any(web.rglob("*.py")), web  # a wrong path must not pass with nothing to check
    offenders = [
        p.name
        for p in web.rglob("*.py")
        if p.name != "service.py" and ENGINE_IMPORT.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
