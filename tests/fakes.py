"""Test doubles shared across the suite's tests."""

from collections.abc import Awaitable, Callable


class FakeSender:
    """A scriptable stand-in for a ``Sender``.

    ``script(text, call_number)`` is awaited on every ``send``. ``call_number`` counts the calls
    made with that same text, starting at 1, so a test can fail a prompt once and then let it
    succeed. The script returns the reply, or raises (``SendError``, any other exception), or
    awaits an ``asyncio.Event`` to hold the call open.

    Tracked for assertions: ``calls`` (the text of every send, in call order), ``in_flight``
    (sends running now), ``peak`` (the highest ``in_flight`` seen) and ``closed``.
    """

    def __init__(self, script: Callable[[str, int], Awaitable[str]]) -> None:
        self._script = script
        self.calls: list[str] = []
        self.in_flight = 0
        self.peak = 0
        self.closed = False

    async def send(self, text: str) -> str:
        self.calls.append(text)
        call_number = self.calls.count(text)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            return await self._script(text, call_number)
        finally:
            self.in_flight -= 1

    async def aclose(self) -> None:
        self.closed = True
