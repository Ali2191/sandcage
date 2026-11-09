"""The model key is sent to the configured endpoint and nowhere else.

urllib follows a 301/302/303 by default and copies every request header except the
content headers to the new address, ``Authorization: Bearer <key>`` included, whatever the
host and even over plain http. Both places that send the key (the model client and the
provider check's model listing) must answer a redirect as the failure it is instead.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sandcage.agent.llm import ModelError, OpenAIChatClient, urllib_transport
from sandcage.provider_check import urllib_get

KEY = "vk-redirect-test-0123456789abcdef"


@contextmanager
def _server(handler: type[BaseHTTPRequestHandler]) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.fixture
def elsewhere() -> Iterator[tuple[str, list[str]]]:
    """Another host's endpoint, recording every Authorization header that reaches it."""
    seen: list[str] = []

    class Record(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            seen.append(self.headers.get("Authorization", ""))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"choices": [{"message": {"content": "hi"}}], "data": []}')

        def do_GET(self) -> None:
            self._answer()

        def do_POST(self) -> None:
            self._answer()

        def log_message(self, *_args: object) -> None:
            return

    with _server(Record) as server:
        yield f"http://127.0.0.1:{server.server_address[1]}/collect", seen


@pytest.fixture
def redirecting(elsewhere: tuple[str, list[str]]) -> Iterator[str]:
    """The configured endpoint, answering every request with a 302 to ``elsewhere``."""
    target = elsewhere[0]

    class Redirect(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            self._answer()

        def do_POST(self) -> None:
            self._answer()

        def log_message(self, *_args: object) -> None:
            return

    with _server(Redirect) as server:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"


@pytest.mark.parametrize("code", [302])
def test_the_model_client_does_not_follow_a_redirect(
    redirecting: str, elsewhere: tuple[str, list[str]], code: int
) -> None:
    client = OpenAIChatClient(base_url=redirecting, api_key=KEY, model="m", retries=0)
    with pytest.raises(ModelError, match=f"HTTP {code}") as raised:
        client.complete([{"role": "user", "content": "hello"}])
    assert elsewhere[1] == []
    assert KEY not in str(raised.value)


def test_the_raw_transport_returns_the_redirect_itself(
    redirecting: str, elsewhere: tuple[str, list[str]]
) -> None:
    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    reply = urllib_transport(redirecting + "/chat/completions", b"{}", headers, 5.0)
    assert reply.status == 302
    assert elsewhere[1] == []


def test_the_provider_checks_model_listing_does_not_follow_a_redirect(
    redirecting: str, elsewhere: tuple[str, list[str]]
) -> None:
    reply = urllib_get(redirecting + "/models", {"Authorization": f"Bearer {KEY}"}, 5.0)
    assert reply.status == 302
    assert elsewhere[1] == []
