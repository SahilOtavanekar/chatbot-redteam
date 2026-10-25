"""The HTTP sender: PyRIT's ``HTTPTarget`` behind our ``Sender`` protocol.

PyRIT supplies the request plumbing only. Three of its defaults would hide failures, so this
module overrides them (see docs/pyrit-spike-findings.md):

* It does not escape the prompt, so a quote breaks the JSON body and the bot's 422 comes back as
  an empty reply. We escape with ``JsonStringConverter`` before sending.
* It never checks the HTTP status. Our callback hands the response to ``extract_reply``, which
  raises ``SendError`` for a non-2xx status, an unreadable body or an empty reply.
* It opens a new connection per prompt unless given a client. One shared ``httpx.AsyncClient``
  serves every send.
"""

import json
from urllib.parse import urlsplit

import httpx
from pyrit.converter import JsonStringConverter
from pyrit.models import Message, MessagePiece
from pyrit.prompt_target import HTTPTarget

from suite.config import PROMPT_PLACEHOLDER, EndpointSpec
from suite.extract import extract_reply
from suite.models import SendError

PROMPT_MARKER = "{PROMPT}"  # PyRIT's default placeholder in a raw request


def build_raw_request(spec: EndpointSpec) -> str:
    """The spec as a raw HTTP request that ``HTTPTarget`` can parse, with ``{PROMPT}`` in the body.

    ``{prompt}`` in the spec's body becomes ``{PROMPT}``. The ``Host`` header is the URL's host
    and port without any ``user:password@`` part, so credentials in a URL are never sent as a
    header value; put credentials in ``headers`` instead.
    """
    if spec.url is None or spec.body is None:
        raise ValueError("an http endpoint needs a url and a body")
    parts = urlsplit(spec.url)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    lines = [f"{spec.method} {target} HTTP/1.1", f"Host: {parts.netloc.rpartition('@')[2]}"]
    if not any(name.lower() == "content-type" for name in spec.headers):
        lines.append("Content-Type: application/json")
    lines.extend(f"{name}: {value}" for name, value in spec.headers.items())
    body = json.dumps(spec.body, ensure_ascii=False).replace(PROMPT_PLACEHOLDER, PROMPT_MARKER)
    return "\n".join([*lines, "", body])


class PyritHttpSender:
    """Sends prompts to an HTTP chatbot through PyRIT's ``HTTPTarget``."""

    def __init__(self, spec: EndpointSpec) -> None:
        assert spec.url is not None  # an http spec always has one; build_raw_request checks too
        limits = spec.limits
        self._spec = spec
        self._converter = JsonStringConverter()
        self._client = httpx.AsyncClient(
            timeout=limits.timeout_s,
            limits=httpx.Limits(
                max_connections=limits.concurrency, max_keepalive_connections=limits.concurrency
            ),
        )
        # max_requests_per_minute stays unset: the engine's RateLimiter owns rate limits.
        self._target = HTTPTarget(
            http_request=build_raw_request(spec),
            use_tls=spec.url.startswith("https"),
            callback_function=self._callback,
            client=self._client,
            follow_redirects=False,
        )

    def _callback(self, *, response: httpx.Response) -> str:
        """PyRIT calls this with every response; the string it returns becomes the reply."""
        return extract_reply(
            status=response.status_code,
            headers=response.headers,
            body=response.content,
            reply_path=self._spec.reply,
        )

    async def send(self, text: str) -> str:
        """Send one prompt and return the reply, or raise ``SendError``."""
        escaped = (await self._converter.convert_async(prompt=text)).output_text
        # No conversation_id: PyRIT then does no memory lookup for this send.
        piece = MessagePiece(role="user", original_value=text, converted_value=escaped)
        try:
            responses = await self._target.send_prompt_async(
                message=Message(message_pieces=[piece])
            )
        except httpx.TimeoutException:
            raise SendError(
                "timeout", f"no reply within {self._spec.limits.timeout_s:g} s"
            ) from None
        except httpx.HTTPError as exc:
            raise SendError("transport", f"{type(exc).__name__}: {exc}") from None
        return responses[0].get_value()

    async def aclose(self) -> None:
        await self._client.aclose()
