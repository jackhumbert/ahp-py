"""The OAuth flow a connector runs, against the real HTTP app.

Discovery, dynamic client registration, PKCE authorization through the owner's
sign-in page, the code exchange, refresh rotation, revocation and the bearer
check on `/mcp`: every step over HTTP, the way a chat app's connector does it.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ahp_mcp import Fleet
from ahp_mcp.auth import MAX_FAILURES, OwnerAuthorizationServer
from ahp_mcp.server import build_server, http_app

PUBLIC = "https://mcp.example.com"
RESOURCE = f"{PUBLIC}/mcp"
PASSPHRASE = "correct horse battery staple"
CALLBACK = "https://app.example.com/oauth/callback"


def _unused() -> Any:
    raise AssertionError("no test here reaches the AHP endpoint")


def _owner(tmp: Path, **options: Any) -> OwnerAuthorizationServer:
    return OwnerAuthorizationServer(
        PASSPHRASE,
        issuer_url=PUBLIC,
        resource=RESOURCE,
        state_path=tmp / "oauth.json",
        **options,
    )


@contextlib.asynccontextmanager
async def served(owner: OwnerAuthorizationServer) -> AsyncIterator[httpx.AsyncClient]:
    server = build_server(Fleet(_unused), public_url=PUBLIC, auth=owner)
    app = http_app(server, bind="127.0.0.1", public_url=PUBLIC)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=PUBLIC) as client:
            yield client


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def _register(client: httpx.AsyncClient, redirect: str = CALLBACK) -> dict[str, Any]:
    response = await client.post(
        "/register",
        json={
            "client_name": "Example App",
            "redirect_uris": [redirect],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert response.status_code == 201, response.text
    registered: dict[str, Any] = response.json()
    return registered


async def _start(client: httpx.AsyncClient, client_id: str, challenge: str) -> str:
    """Up to the sign-in page: the login URL `/authorize` sends the browser to."""
    response = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "resource": RESOURCE,
        },
    )
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(f"{PUBLIC}/login?request=")
    return location


def _request_id(login_url: str) -> str:
    return parse_qs(urlsplit(login_url).query)["request"][0]


async def _sign_in(client: httpx.AsyncClient, login_url: str, passphrase: str) -> httpx.Response:
    return await client.post(
        "/login",
        data={"request": _request_id(login_url), "passphrase": passphrase, "action": "allow"},
    )


async def _tokens(
    client: httpx.AsyncClient, owner_client: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    verifier, challenge = _pkce()
    login = await _start(client, owner_client["client_id"], challenge)
    allowed = await _sign_in(client, login, PASSPHRASE)
    assert allowed.status_code == 302, allowed.text
    back = urlsplit(allowed.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == CALLBACK
    query = parse_qs(back.query)
    assert query["state"] == ["xyz"]
    response = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": CALLBACK,
            "client_id": owner_client["client_id"],
            "code_verifier": verifier,
            "resource": RESOURCE,
        },
    )
    assert response.status_code == 200, response.text
    return response.json(), query["code"][0]


async def _initialize(client: httpx.AsyncClient, token: str | None) -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
    )


async def test_metadata_points_the_connector_at_this_server(tmp_path: Path) -> None:
    async with served(_owner(tmp_path)) as client:
        resource = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
        assert resource["resource"] == RESOURCE
        assert [s.rstrip("/") for s in resource["authorization_servers"]] == [PUBLIC]
        server = (await client.get("/.well-known/oauth-authorization-server")).json()
        assert server["issuer"].rstrip("/") == PUBLIC
        assert server["registration_endpoint"] == f"{PUBLIC}/register"
        assert server["code_challenge_methods_supported"] == ["S256"]


async def test_mcp_without_a_token_is_refused_with_a_pointer(tmp_path: Path) -> None:
    async with served(_owner(tmp_path)) as client:
        response = await _initialize(client, None)
        assert response.status_code == 401
        assert "oauth-protected-resource/mcp" in response.headers["www-authenticate"]
        assert (await _initialize(client, "made-up")).status_code == 401


async def test_the_whole_flow_ends_in_a_working_bearer(tmp_path: Path) -> None:
    async with served(_owner(tmp_path)) as client:
        registered = await _register(client)
        tokens, code = await _tokens(client, registered)
        assert tokens["token_type"].lower() == "bearer"
        response = await _initialize(client, tokens["access_token"])
        assert response.status_code == 200, response.text
        assert "ahp-mcp" in response.text

        # A code is good once.
        again = await client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CALLBACK,
                "client_id": registered["client_id"],
                "code_verifier": "x" * 43,
            },
        )
        assert again.status_code == 400


async def test_the_sign_in_page_names_the_app_and_where_it_goes(tmp_path: Path) -> None:
    async with served(_owner(tmp_path)) as client:
        registered = await _register(client)
        login = await _start(client, registered["client_id"], _pkce()[1])
        page = await client.get(login)
        assert page.status_code == 200
        assert "Example App" in page.text
        assert "app.example.com" in page.text
        assert page.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]


async def test_a_wrong_passphrase_issues_nothing_and_locks_after_a_few(tmp_path: Path) -> None:
    async with served(_owner(tmp_path)) as client:
        registered = await _register(client)
        login = await _start(client, registered["client_id"], _pkce()[1])
        for _ in range(MAX_FAILURES):
            wrong = await _sign_in(client, login, "not the passphrase")
            assert wrong.status_code == 401
            assert "location" not in wrong.headers
        locked = await _sign_in(client, login, PASSPHRASE)
        assert locked.status_code == 429


async def test_deny_sends_the_app_back_with_access_denied(tmp_path: Path) -> None:
    async with served(_owner(tmp_path)) as client:
        registered = await _register(client)
        login = await _start(client, registered["client_id"], _pkce()[1])
        denied = await client.post("/login", data={"request": _request_id(login), "action": "deny"})
        assert denied.status_code == 302
        query = parse_qs(urlsplit(denied.headers["location"]).query)
        assert query["error"] == ["access_denied"]
        # The request is spent: the passphrase cannot be used on it afterwards.
        assert (await _sign_in(client, login, PASSPHRASE)).status_code == 400


async def test_refresh_rotates_and_revoking_ends_the_pair(tmp_path: Path) -> None:
    async with served(_owner(tmp_path)) as client:
        registered = await _register(client)
        tokens, _ = await _tokens(client, registered)

        def refresh(token: str) -> dict[str, str]:
            return {
                "grant_type": "refresh_token",
                "refresh_token": token,
                "client_id": registered["client_id"],
            }

        rotated = await client.post("/token", data=refresh(tokens["refresh_token"]))
        assert rotated.status_code == 200, rotated.text
        fresh = rotated.json()
        assert fresh["access_token"] != tokens["access_token"]
        # The old pair is spent.
        spent = await client.post("/token", data=refresh(tokens["refresh_token"]))
        assert spent.status_code == 400
        assert (await _initialize(client, tokens["access_token"])).status_code == 401
        assert (await _initialize(client, fresh["access_token"])).status_code == 200

        # The SDK's revocation form wants the client_secret field even from a
        # public client, so a public client sends it empty.
        revoked = await client.post(
            "/revoke",
            data={
                "token": fresh["refresh_token"],
                "client_id": registered["client_id"],
                "client_secret": "",
            },
        )
        assert revoked.status_code == 200, revoked.text
        assert (await _initialize(client, fresh["access_token"])).status_code == 401


async def test_the_redirect_allowlist_refuses_other_apps(tmp_path: Path) -> None:
    owner = _owner(tmp_path, allowed_redirect_uris=["https://app.example.com/oauth/*"])
    async with served(owner) as client:
        await _register(client)
        response = await client.post(
            "/register",
            json={"client_name": "Elsewhere", "redirect_uris": ["https://evil.example.net/cb"]},
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_redirect_uri"


async def test_a_restart_keeps_clients_and_tokens_but_stores_only_digests(tmp_path: Path) -> None:
    async with served(_owner(tmp_path)) as client:
        registered = await _register(client)
        tokens, _ = await _tokens(client, registered)
    stored = (tmp_path / "oauth.json").read_text(encoding="utf-8")
    assert tokens["access_token"] not in stored
    assert tokens["refresh_token"] not in stored
    assert PASSPHRASE not in stored
    assert registered["client_id"] in json.loads(stored)["clients"][0]["client_id"]
    async with served(_owner(tmp_path)) as client:
        assert (await _initialize(client, tokens["access_token"])).status_code == 200


def test_a_short_passphrase_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 12"):
        OwnerAuthorizationServer("short", issuer_url=PUBLIC, resource=RESOURCE)
