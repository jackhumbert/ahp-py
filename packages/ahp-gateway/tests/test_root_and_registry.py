"""The merged root channel, and admission in the registry."""

import pytest
from agent_host_protocol import REDUCERS

from agent_host_broker.core.broker import Broker, jittered
from agent_host_broker.core.root import merge_root, node_details, root_actions
from agent_host_broker.registry import NodeRecord, Principal, StaticInventory, is_valid_node_id


def test_agents_merge_with_the_first_node_winning_a_shared_provider() -> None:
    merged = merge_root(
        [
            {"agents": [{"provider": "echo", "displayName": "A"}], "activeSessions": 1},
            {
                "agents": [{"provider": "echo", "displayName": "B"}, {"provider": "x"}],
                "activeSessions": 2,
            },
        ]
    )
    assert merged["agents"] == [{"provider": "echo", "displayName": "A"}, {"provider": "x"}]
    assert merged["activeSessions"] == 3


def test_config_is_never_advertised() -> None:
    merged = merge_root([{"agents": [], "activeSessions": 0, "config": {"schema": {}}}])
    assert "config" not in merged


def test_node_details_carry_a_hosts_extensions_verbatim() -> None:
    # The shape copilotd 0.9.1 was observed to send: sealing keys and project
    # management in root `_meta`, its own settings in `config`.
    keys = [
        {"keyId": "k1", "use": "auth-token", "algorithm": "x25519-sealedbox", "publicKey": "AA"}
    ]
    root = {
        "agents": [],
        "_meta": {"copilot.encryptionKeys": keys, "copilot.projectManagement": {"available": True}},
        "config": {"schema": {"type": "object", "properties": {}}, "values": {"copilot": {}}},
    }
    handshake = {
        "serverInfo": {"name": "copilotd", "version": "0.9.1", "title": "Copilot Host Daemon"}
    }
    details = node_details(handshake, root)
    assert details == {
        "serverInfo": handshake["serverInfo"],
        "meta": root["_meta"],
        "config": root["config"],
    }
    # Copies: a later change to the node's state must not reach a sent snapshot.
    details["meta"]["extra"] = 1
    assert "extra" not in root["_meta"]


def test_node_details_leave_out_what_a_host_did_not_say() -> None:
    assert node_details({}, {"agents": [], "_meta": {}}) == {}


def test_redial_delay_is_spread_by_the_jitter() -> None:
    assert jittered(10.0, 0.25, draw=lambda: 0.0) == pytest.approx(7.5)
    assert jittered(10.0, 0.25, draw=lambda: 0.5) == pytest.approx(10.0)
    assert jittered(10.0, 0.25, draw=lambda: 1.0) == pytest.approx(12.5)
    assert jittered(10.0, 0.0) == 10.0


@pytest.mark.parametrize("jitter", [-0.1, 1.0, 1.5])
def test_a_jitter_outside_zero_to_one_is_refused(jitter: float) -> None:
    with pytest.raises(ValueError, match="redial_jitter"):
        Broker(StaticInventory([]), None, lambda info: None, redial_jitter=jitter)  # type: ignore[arg-type]


def test_root_actions_reduce_the_surface_to_the_merged_state() -> None:
    before = merge_root([{"agents": [], "activeSessions": 0}])
    after = merge_root(
        [{"agents": [{"provider": "p"}], "activeSessions": 2, "terminals": [{"resource": "t"}]}]
    )
    state = dict(before)
    for action in root_actions(before, after):
        state = REDUCERS["root"](state, action)
    assert state == after


def test_no_change_is_no_action() -> None:
    state = merge_root([{"agents": [{"provider": "p"}], "activeSessions": 1}])
    assert root_actions(state, state) == []


def test_admission_is_by_group_and_closed_by_default() -> None:
    inventory = StaticInventory(
        [
            NodeRecord("build-01", "ws://b", frozenset({"dev"})),
            NodeRecord("ops-01", "ws://o", frozenset({"ops"})),
            NodeRecord("unassigned", "ws://u"),
        ]
    )
    dev = Principal("alice", frozenset({"dev"}))
    assert [r.node_id for r in inventory.nodes_for(dev)] == ["build-01"]
    assert inventory.nodes_for(Principal("nobody")) == []


def test_duplicate_node_ids_are_refused() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        StaticInventory([NodeRecord("a", "ws://1"), NodeRecord("a", "ws://2")])


@pytest.mark.parametrize("node_id", ["a", "build-01", "rack1.dc.example"])
def test_valid_node_ids(node_id: str) -> None:
    assert is_valid_node_id(node_id)


@pytest.mark.parametrize("node_id", ["", "A", "-a", "a-", "a/b", "a:1", "localhost", "a..b"])
def test_node_ids_that_cannot_be_a_uri_authority_are_refused(node_id: str) -> None:
    assert not is_valid_node_id(node_id)
    with pytest.raises(ValueError, match="invalid node id"):
        NodeRecord(node_id, "ws://x")
