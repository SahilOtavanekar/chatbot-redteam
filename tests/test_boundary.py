import re
from pathlib import Path


def test_only_adapters_import_pyrit():
    pattern = re.compile(r"^\s*(from|import)\s+pyrit\b", re.M)
    src = Path(__file__).parents[1] / "src" / "suite"
    offenders = [
        p
        for p in src.rglob("*.py")
        if "adapters" not in p.relative_to(src).parts
        and pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
