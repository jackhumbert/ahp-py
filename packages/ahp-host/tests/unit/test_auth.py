"""The `authenticate` command's two failure modes: a leaked token, and a
`-32007` a client cannot act on.

Both are silent. A bearer token that reaches a log, a traceback or a JSON-RPC
error is still a working credential for someone else's service, and nothing in
the host notices. An `AuthRequired` error missing its mandatory `data` looks
like a correct refusal and leaves a client with no resource to go get a token
for -- so it re-prompts, or gives up, and the host's own tests stay green.

Also pinned here: the two places this module knowingly departs from, or takes a
side in, upstream's own ambiguity -- host-global token visibility, and an absent
`scopes` counting as satisfied.
"""

from __future__ import annotations

import json
import traceback

import pytest
from ahp_protocol.types import AHP_ERROR_CODES, ROOT_CHANNEL

from ahp_host.core.auth import (
    AUTH_REQUIRED_METHOD,
    BearerToken,
    ProtectedResource,
    TokenGrant,
    TokenStore,
    auth_required,
    auth_required_params,
    scopes_satisfied,
)

RESOURCE = "https://api.example.invalid"
SECRET = "gho_a_real_looking_secret"

GITHUB = ProtectedResource(
    resource="https://api.github.com",
    resource_name="GitHub Copilot",
    authorization_servers=["https://github.com/login/oauth"],
    scopes_supported=["read:user", "user:email"],
)


class TestWireShapes:
    """`ProtectedResourceMetadata` is RFC 9728, published on `AgentInfo` and fed
    straight back to a client's OAuth code path. A dropped or renamed field is
    invisible here and fatal there."""

    def test_a_declared_resource_matches_the_specification_example(self) -> None:
        assert GITHUB.to_wire() == {
            "resource": "https://api.github.com",
            "resource_name": "GitHub Copilot",
            "authorization_servers": ["https://github.com/login/oauth"],
            "scopes_supported": ["read:user", "user:email"],
            "required": True,
        }

    def test_a_resource_round_trips_through_the_wire(self) -> None:
        assert ProtectedResource.from_wire(GITHUB.to_wire()) == GITHUB

    def test_an_optional_resource_round_trips(self) -> None:
        optional = ProtectedResource(resource=RESOURCE, required=False)
        assert optional.to_wire()["required"] is False
        assert ProtectedResource.from_wire(optional.to_wire()) == optional

    def test_an_absent_required_field_means_required(self) -> None:
        """Clients SHOULD treat absent as `true`; so does the host, and it emits
        the field explicitly rather than betting on the client's default."""
        assert ProtectedResource.from_wire({"resource": RESOURCE}).required is True

    @pytest.mark.parametrize("value", [None, 0, "false", []])
    def test_only_a_literal_false_makes_a_resource_optional(self, value: object) -> None:
        """Failing closed: anything that is not exactly `false` still requires a
        token. An explicit JSON `null` is not an absent key, and neither is a
        string a careless client stringified."""
        assert ProtectedResource.from_wire({"resource": RESOURCE, "required": value}).required

    def test_unmodelled_rfc9728_fields_survive_verbatim(self) -> None:
        """The host reads none of these; a client may read all of them."""
        wire = {
            "resource": RESOURCE,
            "jwks_uri": "https://example.invalid/jwks",
            "bearer_methods_supported": ["header"],
            "resource_tos_uri": "https://example.invalid/tos",
        }
        parsed = ProtectedResource.from_wire(wire)
        assert parsed.extra == {
            "jwks_uri": "https://example.invalid/jwks",
            "bearer_methods_supported": ["header"],
            "resource_tos_uri": "https://example.invalid/tos",
        }
        assert parsed.to_wire() == {**wire, "required": True}

    def test_an_empty_array_is_not_an_absent_key(self) -> None:
        """An empty `authorization_servers` and an absent one are different
        claims, and JavaScript's truthiness collapses them (invariant 5)."""
        empty = ProtectedResource.from_wire({"resource": RESOURCE, "authorization_servers": []})
        absent = ProtectedResource.from_wire({"resource": RESOURCE})
        assert empty.authorization_servers == []
        assert absent.authorization_servers is None
        assert "authorization_servers" in empty.to_wire()
        assert "authorization_servers" not in absent.to_wire()

    def test_a_pass_through_key_cannot_shadow_a_modelled_one(self) -> None:
        resource = ProtectedResource(resource=RESOURCE, extra={"resource": "https://evil.invalid"})
        assert resource.to_wire()["resource"] == RESOURCE

    def test_metadata_without_a_resource_is_rejected(self) -> None:
        """`resource` is RFC 9728's one REQUIRED field; without it the entry
        cannot be correlated with an `authenticate` push at all."""
        with pytest.raises(ValueError, match="resource is required"):
            ProtectedResource.from_wire({"resource_name": "nameless"})


class TestAuthRequiredError:
    """`-32007`'s `data` is a MUST, not a SHOULD. It is the only thing that tells
    a client which resource to authenticate for."""

    def test_the_error_carries_its_mandatory_data(self) -> None:
        error = auth_required([GITHUB])
        assert error.code == AHP_ERROR_CODES["AuthRequired"] == -32007
        assert error.data == {"resources": [GITHUB.to_wire()]}

    def test_the_resources_are_wrapped_not_a_bare_array(self) -> None:
        """`AuthRequiredErrorData` wraps the list so upstream can add fields
        later without breaking the wire shape."""
        assert list(auth_required([GITHUB]).data) == ["resources"]

    def test_data_reaches_the_json_rpc_error_object(self) -> None:
        rendered = auth_required([GITHUB]).to_json()
        assert rendered["code"] == -32007
        assert rendered["data"]["resources"][0]["resource"] == "https://api.github.com"

    def test_an_empty_resource_list_still_emits_data(self) -> None:
        """`AhpError.to_json` drops a `None` data. An empty list is not one --
        the field stays present and correctly shaped."""
        assert auth_required([]).to_json()["data"] == {"resources": []}

    def test_the_default_message_names_the_resource(self) -> None:
        assert auth_required([GITHUB]).message == "Authentication required for GitHub Copilot"

    def test_a_resource_with_no_display_name_falls_back_to_its_identifier(self) -> None:
        assert auth_required([ProtectedResource(RESOURCE)]).message.endswith(RESOURCE)

    def test_the_message_may_be_overridden(self) -> None:
        assert auth_required([GITHUB], "Sign in to continue").message == "Sign in to continue"


class TestAuthRequiredNotification:
    def test_the_params_default_to_the_root_channel(self) -> None:
        assert auth_required_params(ProtectedResource(RESOURCE)) == {
            "channel": ROOT_CHANNEL,
            "resource": {"resource": RESOURCE, "required": True},
            "reason": "required",
        }

    def test_the_resource_is_the_complete_metadata(self) -> None:
        """0.8.0: `resource` is the whole `ProtectedResourceMetadata`, so a client
        can start the OAuth flow from the notification alone."""
        assert auth_required_params(GITHUB)["resource"] == GITHUB.to_wire()

    def test_an_expired_token_is_announced_with_its_own_reason(self) -> None:
        """`expired` and `required` mean different things to a client: one is a
        re-auth of a flow it already completed, the other a first prompt."""
        assert (
            auth_required_params(ProtectedResource(RESOURCE), reason="expired")["reason"]
            == "expired"
        )

    def test_the_notification_may_name_a_non_root_channel(self) -> None:
        params = auth_required_params(ProtectedResource(RESOURCE), channel="ahp-session:/abc")
        assert params["channel"] == "ahp-session:/abc"

    def test_the_method_name_is_the_spelling_the_client_matches_on(self) -> None:
        assert AUTH_REQUIRED_METHOD == "auth/required"


class TestTokenSecrecy:
    """A bearer token that reaches a log line, a traceback or an error response
    is still a working credential, and nothing in the host notices."""

    def test_a_token_is_not_in_its_own_repr(self) -> None:
        assert SECRET not in repr(BearerToken(SECRET))

    def test_a_token_is_not_in_str_or_an_f_string(self) -> None:
        token = BearerToken(SECRET)
        assert SECRET not in str(token)
        assert SECRET not in f"{token}"
        assert SECRET not in "{}".format(token)  # noqa: UP032 - the point is the path
        assert SECRET not in "%s / %r" % (token, token)  # noqa: UP031 - likewise

    def test_the_redaction_says_what_it_is_hiding(self) -> None:
        """A blank is indistinguishable from a bug; the marker keeps an auth
        handshake debuggable without the secret."""
        assert "redacted" in repr(BearerToken(SECRET))

    def test_a_grant_redacts_the_token_it_holds(self) -> None:
        grant = TokenGrant(RESOURCE, BearerToken(SECRET), scopes=("repo",), client_id="vscode")
        assert SECRET not in repr(grant)
        assert RESOURCE in repr(grant), "the resource is what makes the log useful"

    def test_a_formatted_traceback_does_not_leak(self) -> None:
        store = TokenStore()
        grant = store.push(RESOURCE, SECRET, client_id="vscode")
        try:
            raise RuntimeError(f"provider rejected {grant}", grant)
        except RuntimeError as exc:
            rendered = "".join(traceback.format_exception(exc))
        assert SECRET not in rendered
        assert "RuntimeError" in rendered

    def test_a_token_has_no_instance_dict_to_read(self) -> None:
        """`__slots__` closes the `vars()` path, which no amount of `__repr__`
        care would."""
        with pytest.raises(TypeError):
            vars(BearerToken(SECRET))

    def test_an_auth_required_error_cannot_carry_a_token(self) -> None:
        """The helper takes only metadata the host itself published, so there is
        no argument through which a credential could reach the peer."""
        store = TokenStore()
        store.push("https://api.github.com", SECRET, client_id="vscode")
        assert SECRET not in json.dumps(auth_required([GITHUB]).to_json())

    def test_the_only_way_out_is_the_named_one(self) -> None:
        assert BearerToken(SECRET).reveal() == SECRET


class TestScopesSatisfied:
    """The check is advisory -- it decides whether re-prompting the client would
    be pointless, never whether access is permitted."""

    def test_a_superset_grant_satisfies_the_requirement(self) -> None:
        assert scopes_satisfied(["repo", "read:user"], ["repo"])

    def test_an_exact_grant_satisfies_the_requirement(self) -> None:
        assert scopes_satisfied(["repo"], ["repo"])

    def test_a_missing_scope_is_insufficient(self) -> None:
        assert not scopes_satisfied(["read:user"], ["repo"])

    def test_requiring_nothing_is_always_satisfied(self) -> None:
        assert scopes_satisfied([], [])
        assert scopes_satisfied(None, [])

    def test_an_absent_grant_satisfies_anything_by_default(self) -> None:
        """Mirrors the reference host's fallback for clients that never send
        `scopes`. The upstream service is the real enforcement point."""
        assert scopes_satisfied(None, ["repo"])

    def test_an_explicitly_empty_grant_satisfies_nothing(self) -> None:
        """Where we do not mirror the reference: it normalises absent and `[]`
        to the same value, so a client honestly reporting an empty grant gets
        the legacy free pass. Here it is taken at its word."""
        assert not scopes_satisfied([], ["repo"])
        assert not scopes_satisfied((), ["repo"])

    def test_the_fallback_can_be_switched_off(self) -> None:
        assert not scopes_satisfied(None, ["repo"], unscoped_satisfies_any=False)

    def test_scope_order_is_irrelevant(self) -> None:
        assert scopes_satisfied(["b", "a"], ["a", "b"])


class TestTokenStore:
    def test_a_push_is_readable_back_with_its_scopes_and_client(self) -> None:
        store = TokenStore()
        store.push(RESOURCE, SECRET, scopes=["repo", "repo"], client_id="vscode")
        grant = store.get(RESOURCE)
        assert grant is not None
        assert grant.resource == RESOURCE
        assert grant.token.reveal() == SECRET
        assert grant.scopes == ("repo", "repo"), "the peer's claim, not a normalised one"
        assert grant.client_id == "vscode"

    def test_an_omitted_scopes_field_stays_distinguishable_from_an_empty_one(self) -> None:
        store = TokenStore()
        store.push(RESOURCE, SECRET)
        store.push("https://other.invalid", SECRET, scopes=[])
        absent = store.get(RESOURCE)
        empty = store.get("https://other.invalid")
        assert absent is not None
        assert absent.scopes is None
        assert empty is not None
        assert empty.scopes == ()

    def test_a_second_push_for_the_same_resource_replaces_the_first(self) -> None:
        """A refresh is a replacement. The reference keys `(resource, scopes)`
        and so accumulates an entry per scope set, evicting none of them."""
        store = TokenStore()
        store.push(RESOURCE, "first", scopes=["repo"])
        store.push(RESOURCE, "second", scopes=["repo", "read:user"])
        grant = store.get(RESOURCE)
        assert grant is not None
        assert grant.token.reveal() == "second"
        assert grant.scopes == ("repo", "read:user")
        assert store.resources() == (RESOURCE,), "no stale credential is kept alongside"

    def test_an_unauthenticated_resource_has_no_grant(self) -> None:
        assert TokenStore().get(RESOURCE) is None
        assert not TokenStore().satisfies(RESOURCE)

    def test_satisfies_applies_the_stores_own_fallback_setting(self) -> None:
        lenient = TokenStore()
        strict = TokenStore(unscoped_satisfies_any=False)
        for store in (lenient, strict):
            store.push(RESOURCE, SECRET)
        assert lenient.satisfies(RESOURCE, ["repo"])
        assert not strict.satisfies(RESOURCE, ["repo"])
        assert strict.satisfies(RESOURCE), "a token is still a token when nothing is required"

    def test_revoking_drops_the_credential_and_reports_whether_there_was_one(self) -> None:
        store = TokenStore()
        store.push(RESOURCE, SECRET)
        assert store.revoke(RESOURCE) is True
        assert store.revoke(RESOURCE) is False
        assert store.get(RESOURCE) is None

    def test_clear_empties_the_store(self) -> None:
        store = TokenStore()
        store.push(RESOURCE, SECRET)
        store.clear()
        assert store.resources() == ()

    def test_listing_resources_never_exposes_a_token(self) -> None:
        store = TokenStore()
        store.push(RESOURCE, SECRET)
        assert SECRET not in repr(store.resources())


class TestUnsatisfiedResources:
    """What a command consults before it refuses -- the input to `-32007`."""

    def test_a_required_resource_without_a_token_is_listed(self) -> None:
        assert TokenStore().unsatisfied([GITHUB]) == [GITHUB]

    def test_a_resource_with_a_token_is_not_listed(self) -> None:
        store = TokenStore()
        store.push(GITHUB.resource, SECRET)
        assert store.unsatisfied([GITHUB]) == []

    def test_an_optional_resource_is_never_listed(self) -> None:
        """`required=False` means the agent works without it. Refusing over one
        would invent a requirement the host itself advertised as optional."""
        optional = ProtectedResource(resource=RESOURCE, required=False)
        assert TokenStore().unsatisfied([optional]) == []

    def test_the_result_feeds_straight_into_the_error(self) -> None:
        store = TokenStore()
        error = auth_required(store.unsatisfied([GITHUB, ProtectedResource(RESOURCE)]))
        assert [r["resource"] for r in error.data["resources"]] == [
            "https://api.github.com",
            RESOURCE,
        ]


class TestCrossClientVisibility:
    """Upstream contradicts itself: the specification's rationale says auth is
    per-connection, the reference host keys one global map and is given no
    client identity to key by. We match the reference, so this is behaviour a
    multi-tenant embedder must know about rather than discover."""

    def test_a_token_pushed_by_one_client_is_visible_to_every_other(self) -> None:
        store = TokenStore()
        store.push(RESOURCE, SECRET, client_id="alice")
        # Nothing on the lookup path takes a client id, so bob's turn runs on
        # alice's credential.
        assert store.satisfies(RESOURCE)
        grant = store.get(RESOURCE)
        assert grant is not None
        assert grant.client_id == "alice", "recorded for audit, not used to partition"

    def test_a_second_clients_push_overwrites_the_first_clients_token(self) -> None:
        store = TokenStore()
        store.push(RESOURCE, "alices-token", client_id="alice")
        store.push(RESOURCE, "bobs-token", client_id="bob")
        grant = store.get(RESOURCE)
        assert grant is not None
        assert grant.token.reveal() == "bobs-token"
        assert grant.client_id == "bob"

    def test_partitioning_costs_one_store_per_trust_domain(self) -> None:
        """The documented escape hatch. The module will not fake it internally,
        because a store that partitioned would diverge from the wire behaviour
        every known client expects."""
        alice, bob = TokenStore(), TokenStore()
        alice.push(RESOURCE, SECRET, client_id="alice")
        assert bob.get(RESOURCE) is None


class TestExpiry:
    """`AuthenticateParams.expiresIn` (1.0.0), on the store's own clock."""

    def _store(self) -> tuple[TokenStore, list[float]]:
        now = [100.0]
        return TokenStore(clock=lambda: now[0]), now

    def test_a_grant_records_its_deadline(self) -> None:
        store, _ = self._store()
        grant = store.push(RESOURCE, SECRET, expires_in=60)
        assert grant.expires_at == 160.0

    def test_no_expires_in_means_no_deadline(self) -> None:
        store, now = self._store()
        store.push(RESOURCE, SECRET)
        now[0] = 1e9
        assert store.get(RESOURCE) is not None

    def test_an_expired_grant_reads_as_absent_everywhere(self) -> None:
        store, now = self._store()
        store.push(RESOURCE, SECRET, expires_in=60)
        now[0] = 160.0
        assert store.get(RESOURCE) is None
        assert not store.satisfies(RESOURCE)
        assert store.resources() == ()
        required = ProtectedResource(RESOURCE)
        assert store.unsatisfied([required]) == [required]

    def test_revoke_grant_spares_a_newer_push(self) -> None:
        store, _ = self._store()
        old = store.push(RESOURCE, "old", expires_in=60)
        store.push(RESOURCE, "new")
        assert not store.revoke_grant(old)
        grant = store.get(RESOURCE)
        assert grant is not None
        assert grant.token.reveal() == "new"


_A = {"session": "echo:/a", "customization_id": "srv"}
_B = {"session": "echo:/b", "customization_id": "srv"}


class TestTargetedGrants:
    """`authenticate.target` (c5628cd4): a token for ONE MCP server in ONE session.

    "The host MUST apply the token only to that MCP server in that session,
    never to sibling servers, other sessions, or agent-level resources, even if
    they share the resource and scopes." The store is where that is enforceable
    for good: `get(resource)` is the agent-level read every existing caller
    makes, so it must be structurally unable to return a targeted grant.
    """

    def test_get_never_returns_a_targeted_grant(self) -> None:
        store = TokenStore()
        store.push(RESOURCE, "targeted", **_A)
        assert store.get(RESOURCE) is None
        assert not store.satisfies(RESOURCE)
        assert store.resources() == (), "agent-level views are untargeted only"
        needed = ProtectedResource(RESOURCE)
        assert store.unsatisfied([needed]) == [needed]

    def test_get_for_prefers_the_targeted_grant_then_falls_back_to_the_untargeted_one(self) -> None:
        store = TokenStore()
        assert store.get_for(RESOURCE, **_A) is None
        store.push(RESOURCE, "agent-level")
        fallback = store.get_for(RESOURCE, **_A)
        assert fallback is not None
        assert fallback.token.reveal() == "agent-level"
        store.push(RESOURCE, "targeted", **_A)
        own = store.get_for(RESOURCE, **_A)
        assert own is not None
        assert own.token.reveal() == "targeted"
        assert own.session == _A["session"]
        assert own.customization_id == "srv"
        other = store.get_for(RESOURCE, **_B)
        assert other is not None
        assert other.token.reveal() == "agent-level", "a sibling never sees A's grant"

    def test_another_session_or_server_has_no_grant_without_an_untargeted_one(self) -> None:
        store = TokenStore()
        store.push(RESOURCE, "targeted", **_A)
        assert store.get_for(RESOURCE, **_B) is None
        assert store.get_for(RESOURCE, session=_A["session"], customization_id="other") is None

    def test_targeted_and_untargeted_pushes_neither_replace_nor_revoke_each_other(self) -> None:
        store = TokenStore()
        agent = store.push(RESOURCE, "agent-level")
        store.push(RESOURCE, "targeted", **_A)
        assert store.get(RESOURCE) is agent

        assert store.revoke(RESOURCE, **_A) is True
        assert store.get(RESOURCE) is agent, "a targeted revoke leaves the untargeted grant"
        assert store.revoke(RESOURCE, **_A) is False

        targeted = store.push(RESOURCE, "targeted again", **_A)
        assert store.revoke(RESOURCE) is True
        assert store.get(RESOURCE) is None
        assert store.get_for(RESOURCE, **_A) is targeted, "and the reverse"

    def test_revoke_grant_works_per_key(self) -> None:
        store = TokenStore()
        agent = store.push(RESOURCE, "agent-level")
        targeted = store.push(RESOURCE, "targeted", **_A)
        assert store.revoke_grant(targeted) is True
        assert store.revoke_grant(targeted) is False
        assert store.get(RESOURCE) is agent
        replaced = store.push(RESOURCE, "targeted", **_A)
        assert store.revoke_grant(targeted) is False, "a refreshed grant survives its old timer"
        assert store.get_for(RESOURCE, **_A) is replaced

    def test_an_expired_targeted_grant_is_dropped_without_touching_the_untargeted_one(self) -> None:
        now = [0.0]
        store = TokenStore(clock=lambda: now[0])
        agent = store.push(RESOURCE, "agent-level")
        store.push(RESOURCE, "targeted", expires_in=10, **_A)
        now[0] = 11.0
        assert store.get_for(RESOURCE, **_A) is agent
        assert store.get(RESOURCE) is agent

    def test_a_targeted_grant_reprs_without_its_token(self) -> None:
        grant = TokenStore().push(RESOURCE, SECRET, **_A)
        assert SECRET not in repr(grant)

    def test_targets_lists_keys_never_tokens(self) -> None:
        store = TokenStore()
        store.push(RESOURCE, SECRET)
        store.push(RESOURCE, SECRET, **_A)
        assert store.targets() == ((RESOURCE, "echo:/a", "srv"),)
        assert SECRET not in repr(store.targets())

    def test_a_half_specified_target_is_refused(self) -> None:
        store = TokenStore()
        with pytest.raises(ValueError, match="AND a customization_id"):
            store.push(RESOURCE, SECRET, session="echo:/a")
        with pytest.raises(ValueError, match="AND a customization_id"):
            store.get_for(RESOURCE, session="echo:/a", customization_id=None)  # type: ignore[arg-type]
