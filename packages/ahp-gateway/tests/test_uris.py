"""The aggregated namespace: file URIs carry their node, channels do not."""

import pytest

from agent_host_broker.core.uris import (
    ChannelOwners,
    ForeignUriError,
    file_authority,
    learn_owned_channels,
    qualify_file_uris,
    unqualify_file_uris,
)


def test_a_node_file_uri_gains_the_node_as_its_authority() -> None:
    assert qualify_file_uris("file:///home/dev/x.py", "node-a") == "file://node-a/home/dev/x.py"


def test_windows_drive_paths_qualify_the_same_way() -> None:
    assert qualify_file_uris("file:///c%3A/work", "box") == "file://box/c%3A/work"


def test_qualification_reaches_every_nested_string() -> None:
    payload = {"summary": {"workingDirectories": ["file:///srv", "file:///tmp"]}, "n": 3}
    assert qualify_file_uris(payload, "n1") == {
        "summary": {"workingDirectories": ["file://n1/srv", "file://n1/tmp"]},
        "n": 3,
    }


def test_prose_mentioning_a_path_is_left_alone() -> None:
    # Only whole values are URIs; rewriting inside text would change what the
    # agent said.
    text = "I edited file:///home/dev/x.py for you"
    assert qualify_file_uris(text, "n1") == text


def test_channel_uris_pass_through_verbatim() -> None:
    for uri in ("echo:/1234", "ahp-chat://c/abc", "ahp-root://"):
        assert qualify_file_uris(uri, "n1") == uri


def test_unqualify_is_the_inverse() -> None:
    original = {"uri": "file:///a/b", "list": ["file:///c"]}
    assert unqualify_file_uris(qualify_file_uris(original, "n1"), "n1") == original


def test_a_file_on_another_node_is_refused_not_forwarded() -> None:
    with pytest.raises(ForeignUriError):
        unqualify_file_uris({"uri": "file://node-b/etc/passwd"}, "node-a")


def test_an_unqualified_file_uri_is_the_routed_nodes_own() -> None:
    assert unqualify_file_uris("file:///tmp", "node-a") == "file:///tmp"


def test_file_authority() -> None:
    assert file_authority("file://node-a/x") == "node-a"
    assert file_authority("file:///x") is None
    assert file_authority("echo:/x") is None
    assert file_authority(3) is None


def test_owned_channels_are_learned_from_any_depth() -> None:
    state = {
        "resource": "echo:/s1",
        "chats": [{"resource": "ahp-chat://c1"}, {"resource": "ahp-chat://c2"}],
        "annotations": {"resource": "echo:/s1/annotations"},
        "cwd": {"resource": "file://n1/tmp"},
        "channel": "ahp-root://",
    }
    assert learn_owned_channels(state) == {
        "echo:/s1",
        "ahp-chat://c1",
        "ahp-chat://c2",
        "echo:/s1/annotations",
    }


def test_the_first_owner_keeps_a_channel() -> None:
    owners = ChannelOwners()
    owners.claim("a", {"x"})
    owners.claim("b", {"x", "y"})
    assert owners.owner_of("x") == "a"
    assert owners.owner_of("y") == "b"
