"""Adapters: the only package that imports pyrit.

``build_sender`` turns an endpoint spec into a ``Sender`` and ``build_detectors`` into the
judge's detectors. Callers must ``await init_pyrit()`` first: PyRIT targets and scorers use its
central memory.
"""

from pyrit.setup import initialize_pyrit_async

from suite.adapters.detectors import pyrit_detectors
from suite.adapters.gandalf import GandalfPasswordCheck, PyritGandalfSender
from suite.adapters.http import PyritHttpSender
from suite.config import EndpointSpec
from suite.engine import RateLimiter, Sender
from suite.judge import Detector

GANDALF_PASSWORD = "gandalf_password"

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
    if spec.type == "gandalf":
        return PyritGandalfSender(spec)
    raise ValueError(f"no sender for endpoint type {spec.type!r}")


def build_detectors(
    spec: EndpointSpec, limiter: RateLimiter
) -> tuple[dict[str, Detector], tuple[str, ...]]:
    """The detectors for an endpoint, and the names its judge must apply to every reply.

    Every endpoint gets PyRIT's regex detectors. A Gandalf endpoint also gets
    ``gandalf_password``, which is forced on every reply: beating a level is the point of the
    challenge, whatever the prompt's own checks say. Its guesses share ``limiter``. Needs
    ``await init_pyrit()`` to have run.
    """
    detectors = pyrit_detectors()
    if spec.type != "gandalf":
        return detectors, ()
    assert spec.level is not None  # config validation guarantees a level for gandalf
    detectors[GANDALF_PASSWORD] = GandalfPasswordCheck(spec.level, limiter)
    return detectors, (GANDALF_PASSWORD,)
