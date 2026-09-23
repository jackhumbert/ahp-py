"""The merged root channel, and admission in the registry."""

import pytest
from agent_host_protocol import REDUCERS

from agent_host_broker.core.root import merge_root, root_actions
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
