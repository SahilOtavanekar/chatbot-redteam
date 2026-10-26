"""PyRIT's bundled garak datasets: ``.prompt`` files loaded into plain rows.

PyRIT ships NVIDIA garak's probe prompts as ``SeedDataset`` YAML files. ``load_garak_dataset``
reads one and hands back ``DatasetPrompt`` rows, so nothing outside ``suite.adapters`` sees a
PyRIT type. Loading is a model method that reads a file: it touches no PyRIT memory, so it needs
no ``init_pyrit()`` and no network.
"""

from dataclasses import dataclass
from pathlib import Path

import pyrit
from pyrit.models import SeedDataset

from suite.models import ConfigError

GARAK_DIR = Path(pyrit.__file__).parent / "datasets" / "seed_datasets" / "local" / "garak"


@dataclass(frozen=True)
class DatasetPrompt:
    """One prompt of a dataset; ``index`` is its 0-based position in the file."""

    dataset: str
    source: str
    text: str
    index: int


def load_garak_dataset(name: str, *, base_dir: Path = GARAK_DIR) -> list[DatasetPrompt]:
    """The prompts of ``<base_dir>/<name>.prompt``, in file order.

    ``source`` is where the prompt came from (garak's file URL): the seed's own, else the
    dataset's, else ``""``. A missing, unparsable or empty file raises ``ConfigError`` naming it.
    The message carries the error's type only, never its text, which could quote a prompt.
    """
    path = base_dir / f"{name}.prompt"
    if not path.is_file():
        raise ConfigError(f"garak dataset {name!r} not found: no file {path}")
    try:
        dataset = SeedDataset.from_yaml_file(path)
    except (OSError, ValueError) as exc:  # pydantic's ValidationError is a ValueError
        raise ConfigError(
            f"cannot load garak dataset {path}: not a valid, non-empty prompt file "
            f"({type(exc).__name__})"
        ) from None
    if not dataset.seeds:
        raise ConfigError(f"garak dataset {path} has no prompts")
    return [
        DatasetPrompt(
            dataset=name,
            source=seed.source or dataset.source or "",
            text=seed.value,
            index=index,
        )
        for index, seed in enumerate(dataset.seeds)
    ]
