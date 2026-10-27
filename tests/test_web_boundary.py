import re
from pathlib import Path


def test_only_service_imports_engine():
    """Within ``suite.web``, only ``service.py`` may import an engine module.

    The routers and views go through the service layer, so the engine stays behind one seam.
    """
    pattern = re.compile(
        r"^\s*(from|import)\s+suite\.(report|review|compare|evidence|prompts)\b", re.M
    )
    web = Path(__file__).parents[1] / "src" / "suite" / "web"
    assert any(web.rglob("*.py")), web  # a wrong path must not pass with nothing to check
    offenders = [
        p.name
        for p in web.rglob("*.py")
        if p.name != "service.py" and pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
