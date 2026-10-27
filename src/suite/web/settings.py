"""Web server settings. Pure data: this module imports nothing from the engine."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WebSettings:
    """Where the UI finds run folders and where it listens.

    ``results_dir`` is the folder that holds one sub-folder per run. The default host is the
    loopback address, so the UI is reachable only from this machine unless the operator opts in.
    """

    results_dir: Path
    host: str = "127.0.0.1"
    port: int = 8787
