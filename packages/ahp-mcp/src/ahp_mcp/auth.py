"""The OAuth half: a small authorization server with one owner.

A chat app adds a remote MCP server as a connector by running the MCP
authorization flow against it: discover the metadata, register itself
(dynamic client registration), send the person to `/authorize` with PKCE,
swap the code for tokens, refresh them later. The `mcp` SDK serves every one
of those endpoints and asks a provider - this class - to decide.

The decision is the owner's. `/authorize` sends the browser to `/login`, a
page naming the app that asked and where it will send the code, with one
field: the owner's passphrase. Nothing is issued without it, so anyone may
register a client (the flow needs that) and still get nothing.

What it keeps, and where:

* Pending sign-ins and codes live in memory, for minutes.
* Registered clients and issued tokens are written to *state_path*, so a
  restart does not make the app sign in again. Tokens are stored as SHA-256
  digests: a leaked state file names nobody's live token.

Failed passphrases are counted: after `MAX_FAILURES` within
`FAILURE_WINDOW` seconds the page refuses every attempt until the window ends.

This module knows nothing of AHP; it only answers "who is this bearer".
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import html
import json
import os
import secrets
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    IdentityAssertionParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

__all__ = ["LOGIN_PATH", "OWNER", "OwnerAuthorizationServer"]

#: The path of the sign-in page `authorize` sends the browser to.
LOGIN_PATH: Final = "/login"
#: The subject of every token: there is one owner.
OWNER: Final = "owner"

MAX_FAILURES: Final = 5
FAILURE_WINDOW: Final = 15 * 60.0

_PENDING_TTL: Final = 10 * 60.0
_CODE_TTL: Final = 5 * 60.0
_MIN_PASSPHRASE: Final = 12


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@dataclass
class _Pending:
    client_id: str
    params: AuthorizationParams
    expires_at: float


class OwnerAuthorizationServer:
    """An `OAuthAuthorizationServerProvider` whose only resource owner signs in
    with a passphrase.

    *resource* is the MCP endpoint's URL: every token is issued for it, so the
    bearer check can refuse a token minted for anything else.
    *allowed_redirect_uris*, when given, is the list of redirect URIs a client
    may register (exact matches, or a prefix ending in `*`); registering any
    other is refused. Without it any client may register, and the sign-in page
    is the only gate.
    """

    def __init__(
        self,
        passphrase: str,
        *,
        issuer_url: str,
        resource: str,
        state_path: Path | None = None,
        allowed_redirect_uris: Iterable[str] | None = None,
        access_token_ttl: int = 3600,
        refresh_token_ttl: int = 30 * 24 * 3600,
        clock: Any = time.time,
    ) -> None:
        if len(passphrase) < _MIN_PASSPHRASE:
            raise ValueError(f"the owner passphrase must be at least {_MIN_PASSPHRASE} characters")
        self._passphrase = _digest(passphrase)
        self.issuer_url = issuer_url.rstrip("/")
        self.resource = resource
        self.state_path = state_path
        self.allowed_redirect_uris = tuple(allowed_redirect_uris or ())
        self.access_token_ttl = access_token_ttl
        self.refresh_token_ttl = refresh_token_ttl
        self._clock = clock
        self._clients: dict[str, OAuthClientInformationFull] = {}
        #: Keyed by digest. Values are the token models minus the raw token.
        self._access: dict[str, dict[str, Any]] = {}
        self._refresh: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, _Pending] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._failures: list[float] = []
        self._load()

    # ─── registration ────────────────────────────────────────────────────

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise RegistrationError("invalid_client_metadata", "no client_id was assigned")
        if self.allowed_redirect_uris:
            for uri in client_info.redirect_uris or []:
                if not self._redirect_allowed(str(uri)):
                    raise RegistrationError(
                        "invalid_redirect_uri", f"{uri} is not a redirect URI this server allows"
                    )
        self._clients[client_info.client_id] = client_info
        self._save()

    def _redirect_allowed(self, uri: str) -> bool:
        for allowed in self.allowed_redirect_uris:
            if allowed.endswith("*") and uri.startswith(allowed[:-1]):
                return True
            if uri == allowed:
                return True
        return False

    # ─── the owner's sign-in ─────────────────────────────────────────────

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if not client.client_id:
            raise AuthorizeError("unauthorized_client", "the client has no id")
        self._prune()
        request_id = secrets.token_urlsafe(32)
        self._pending[request_id] = _Pending(client.client_id, params, self._clock() + _PENDING_TTL)
        return f"{self.issuer_url}{LOGIN_PATH}?request={request_id}"

    def routes(self) -> list[Route]:
        """The sign-in page, to mount beside the SDK's own auth routes."""
        return [Route(LOGIN_PATH, self._login, methods=["GET", "POST"])]

    async def _login(self, request: Request) -> Response:
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            pending = self._live_pending(request_id)
            if pending is None:
                return _page("This sign-in link has expired. Start again from the app.", 400)
            return self._form(request_id, pending)
        form = await request.form()
        request_id = str(form.get("request", ""))
        pending = self._live_pending(request_id)
        if pending is None:
            return _page("This sign-in link has expired. Start again from the app.", 400)
        redirect = str(pending.params.redirect_uri)
        state = pending.params.state
        if form.get("action") == "deny":
            del self._pending[request_id]
            return _redirect(construct_redirect_uri(redirect, error="access_denied", state=state))
        if self._locked():
            return _page("Too many wrong passphrases. Wait a while and try again.", 429)
        given = str(form.get("passphrase", ""))
        if not hmac.compare_digest(_digest(given), self._passphrase):
            self._failures.append(self._clock())
            return self._form(request_id, pending, error="That passphrase is not right.")
        del self._pending[request_id]
        self._failures.clear()
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=pending.params.scopes or [],
            expires_at=self._clock() + _CODE_TTL,
            client_id=pending.client_id,
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            resource=self.resource,
            subject=OWNER,
        )
        return _redirect(construct_redirect_uri(redirect, code=code, state=state))

    def _live_pending(self, request_id: str) -> _Pending | None:
        self._prune()
        return self._pending.get(request_id) if request_id else None

    def _locked(self) -> bool:
        cutoff = self._clock() - FAILURE_WINDOW
        self._failures = [t for t in self._failures if t > cutoff]
        return len(self._failures) >= MAX_FAILURES

    def _form(self, request_id: str, pending: _Pending, error: str | None = None) -> Response:
        client = self._clients.get(pending.client_id)
        name = (client.client_name if client else None) or "An app"
        target = urlsplit(str(pending.params.redirect_uri)).netloc or "an unknown address"
        notice = f'<p class="error">{html.escape(error)}</p>' if error else ""
        body = f"""
<h1>Allow {html.escape(name)}?</h1>
<p>{html.escape(name)} wants to start, read and steer agent sessions on your machines.
If you allow it, it is sent back to <strong>{html.escape(target)}</strong>.</p>
{notice}
<form method="post">
  <input type="hidden" name="request" value="{html.escape(request_id)}">
  <label>Passphrase <input type="password" name="passphrase" autocomplete="current-password"
    autofocus></label>
  <button type="submit" name="action" value="allow">Allow</button>
  <button type="submit" name="action" value="deny">Deny</button>
</form>"""
        return _page(body, 401 if error else 200, raw=True)

    # ─── codes and tokens ────────────────────────────────────────────────

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        if code.expires_at < self._clock():
            del self._codes[authorization_code]
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "the authorization code was already used")
        return self._issue(authorization_code.client_id, authorization_code.scopes)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        record = self._refresh.get(_digest(refresh_token))
        if record is None or record["client_id"] != client.client_id:
            return None
        if record["expires_at"] is not None and record["expires_at"] < self._clock():
            self._forget(refresh_digest=_digest(refresh_token))
            return None
        fields = {k: v for k, v in record.items() if k != "access"}
        return RefreshToken(token=refresh_token, **fields)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        # Rotated: the refresh token is spent, and so is the access token it was issued with.
        self._forget(refresh_digest=_digest(refresh_token.token))
        return self._issue(refresh_token.client_id, scopes or refresh_token.scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        record = self._access.get(_digest(token))
        if record is None:
            return None
        if record["expires_at"] is not None and record["expires_at"] < self._clock():
            self._forget(access_digest=_digest(token))
            return None
        fields = {k: v for k, v in record.items() if k != "refresh"}
        return AccessToken(token=token, **fields)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            self._forget(access_digest=_digest(token.token))
        else:
            self._forget(refresh_digest=_digest(token.token))

    async def exchange_identity_assertion(
        self, client: OAuthClientInformationFull, params: IdentityAssertionParams
    ) -> OAuthToken:
        """No enterprise identity provider is trusted here: the owner signs in themselves."""
        raise TokenError("unsupported_grant_type", "this server does not take identity assertions")

    def _issue(self, client_id: str, scopes: list[str]) -> OAuthToken:
        now = int(self._clock())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        access_digest, refresh_digest = _digest(access), _digest(refresh)
        common = {"client_id": client_id, "scopes": list(scopes), "resource": self.resource}
        self._access[access_digest] = {
            **common,
            "expires_at": now + self.access_token_ttl,
            "subject": OWNER,
            "refresh": refresh_digest,
        }
        self._refresh[refresh_digest] = {
            **common,
            "expires_at": now + self.refresh_token_ttl,
            "subject": OWNER,
            "access": access_digest,
        }
        self._save()
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.access_token_ttl,
            refresh_token=refresh,
            scope=" ".join(scopes) or None,
        )

    def _forget(
        self, *, access_digest: str | None = None, refresh_digest: str | None = None
    ) -> None:
        """Drop a token and its partner: revoking either half revokes the pair."""
        if access_digest is not None:
            record = self._access.pop(access_digest, None)
            if record is not None:
                self._refresh.pop(record.get("refresh") or "", None)
        if refresh_digest is not None:
            record = self._refresh.pop(refresh_digest, None)
            if record is not None:
                self._access.pop(record.get("access") or "", None)
        self._save()

    def _prune(self) -> None:
        now = self._clock()
        for request_id in [k for k, p in self._pending.items() if p.expires_at < now]:
            del self._pending[request_id]
        for code in [k for k, c in self._codes.items() if c.expires_at < now]:
            del self._codes[code]

    # ─── persistence ─────────────────────────────────────────────────────

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        for raw in data.get("clients") or []:
            client = OAuthClientInformationFull.model_validate(raw)
            if client.client_id:
                self._clients[client.client_id] = client
        now = self._clock()
        self._access = {
            k: v
            for k, v in (data.get("access") or {}).items()
            if v.get("expires_at") is None or v["expires_at"] > now
        }
        self._refresh = {
            k: v
            for k, v in (data.get("refresh") or {}).items()
            if v.get("expires_at") is None or v["expires_at"] > now
        }

    def _save(self) -> None:
        if self.state_path is None:
            return
        data = {
            "clients": [
                c.model_dump(mode="json", exclude_none=True) for c in self._clients.values()
            ],
            "access": self._access,
            "refresh": self._refresh,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.state_path.parent, prefix=".ahp-mcp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
            with contextlib.suppress(OSError):
                os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise


_HEADERS: Final = {
    # The sign-in page must not be framed: a framed page is a clickjacked Allow.
    "X-Frame-Options": "DENY",
    # No `form-action`: browsers apply it to the redirect after the post too,
    # and that redirect goes to the app's own callback, on another origin.
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
    "frame-ancestors 'none'",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}

_STYLE: Final = (
    "body{font-family:system-ui,sans-serif;max-width:28rem;margin:4rem auto;padding:0 1rem}"
    "label{display:block;margin:1rem 0}input{width:100%;padding:.5rem;margin-top:.25rem}"
    "button{padding:.5rem 1rem;margin-right:.5rem}.error{color:#b00}"
)


def _page(body: str, status: int, *, raw: bool = False) -> HTMLResponse:
    content = body if raw else f"<p>{html.escape(body)}</p>"
    document = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Sign in</title><style>{_STYLE}</style></head><body>{content}</body></html>"
    )
    return HTMLResponse(document, status_code=status, headers=_HEADERS)


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})
