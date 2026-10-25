"""Adapters: the only package that imports pyrit.

``build_sender`` turns an endpoint spec into a ``Sender``. Callers must ``await init_pyrit()``
first: PyRIT targets look up its central memory when they are constructed.
"""

from pyrit.setup import initialize_pyrit_async

from suite.adapters.http import PyritHttpSender
from suite.config import EndpointSpec
from suite.engine import Sender

_pyrit_ready = False


async def init_pyrit() -> None:
    """Initialise PyRIT's in-memory central memory once per process; later calls do nothing."""
    global _pyrit_ready
    if _pyrit_ready:
        return
    await initialize_pyrit_async(memory_db_type="InMemory", load_defaults=False, silent=True)
    _pyrit_ready = True


def build_sender(spec: EndpointSpec) -> Sender:
    """Build the sender for an endpoint spec. Needs ``await init_pyrit()`` to have run."""
    if spec.type == "http":
        return PyritHttpSender(spec)
    raise ValueError(f"no sender for endpoint type {spec.type!r}")
