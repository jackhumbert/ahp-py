"""The connection token must not survive into a log file.

`?tkn=` is a bearer credential in a query string. `test_denied_reason.py` already
says so about *someone else's* proxy -- "a query string is written to a proxy's
access log and a credential should not be" -- while this server logged the whole
upgrade path itself, twice, one of them at WARNING, which is on by default.

A log outlives the connection it describes. It gets shipped to an aggregator,
tailed over a shoulder and pasted into a bug report, and every one of those is a
copy of a live credential that nobody was thinking about.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from agent_host_server import Host, LoopbackSingleUserPolicy
from agent_host_server.provider import EchoProvider
from agent_host_server.ws.server import WebSocketServer, _safe_path

pytestmark = pytest.mark.anyio

SECRET = "s3cr3t-token-value"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Request:
    def __init__(self, path: str) -> None:
        self.path = path
        self.headers: dict[str, str] = {}


class _Connection:
    """Just enough of a websockets connection to record the refusal."""

    def __init__(self) -> None:
        self.responded: Any = None

    def respond(self, status: Any, body: str) -> Any:
        self.responded = (status, body)
        return self.responded


def test_the_token_is_substituted_and_the_rest_of_the_path_survives() -> None:
    """What is left is the diagnostic: "a token was sent" and "none was" are
    the two cases anyone reading a rejected handshake is telling apart."""
    assert _safe_path(f"/?tkn={SECRET}") == "/?tkn=<redacted>"
    assert _safe_path(f"/ahp?tkn={SECRET}&v=2") == "/ahp?tkn=<redacted>&v=2"
    assert _safe_path(f"/ahp?v=2&tkn={SECRET}") == "/ahp?v=2&tkn=<redacted>"
    # A path with no token is not a path with an empty one.
    assert _safe_path("/ahp?v=2") == "/ahp?v=2"
    assert _safe_path("/") == "/"


async def test_a_refused_handshake_logs_no_token(caplog: pytest.LogCaptureFixture) -> None:
    host = Host(EchoProvider(), LoopbackSingleUserPolicy())
    server = WebSocketServer(host, connection_token="the-real-one")
    connection = _Connection()

    with caplog.at_level(logging.DEBUG, logger="agent_host_server.ws.server"):
        await server._process_request(connection, _Request(f"/?tkn={SECRET}"))

    assert connection.responded is not None, "the handshake was not refused"
    # Every record, formatted the way a handler would -- the token could be in
    # the message or in an argument, and only the rendered line is what lands
    # in the file.
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert rendered, "nothing was logged, so this test proves nothing"
    assert SECRET not in rendered, f"the connection token reached a log record:\n{rendered}"
    assert "tkn=<redacted>" in rendered


async def test_an_accepted_handshake_logs_no_token_either(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The DEBUG line runs before the decision, so success is the path that
    leaks a *valid* credential rather than a rejected one."""
    host = Host(EchoProvider(), LoopbackSingleUserPolicy())
    server = WebSocketServer(host, connection_token=SECRET)
    connection = _Connection()

    with caplog.at_level(logging.DEBUG, logger="agent_host_server.ws.server"):
        result = await server._process_request(connection, _Request(f"/?tkn={SECRET}"))

    assert result is None, "a valid token was refused"
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert rendered, "nothing was logged, so this test proves nothing"
    assert SECRET not in rendered, f"the connection token reached a log record:\n{rendered}"


async def test_the_public_attribute_carries_no_token() -> None:
    """`last_handshake_path` is public: whatever an embedder does with it is a
    second copy of the credential in a place nobody audited."""
    host = Host(EchoProvider(), LoopbackSingleUserPolicy())
    server = WebSocketServer(host, connection_token=SECRET)

    await server._process_request(_Connection(), _Request(f"/?tkn={SECRET}"))

    assert server.last_handshake_path is not None
    assert SECRET not in server.last_handshake_path
