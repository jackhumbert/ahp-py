"""The aggregated namespace: file URIs carry their node, channels do not."""

import pytest

from agent_host_broker.core.uris import (
    VIRTUAL_ROOT,
    ChannelOwners,
    ForeignUriError,
    is_virtual_root,
    learn_owned_channels,
    node_of,
    qualify_file_uris,
    root_of,
    unqualify_file_uris,
)

ROOT = "home/dev/src"


def test_a_path_under_the_nodes_root_is_relative_to_it() -> None:
    assert qualify_file_uris("file:///home/dev/src/app/x.py", "node-a", ROOT) == (
        "ahp-file:///node-a/app/x.py"
    )
    # The root itself is the node's own directory in the tree.
    assert qualify_file_uris("file:///home/dev/src", "node-a", ROOT) == "ahp-file:///node-a"
    assert qualify_file_uris("file:///home/dev/src/", "node-a", ROOT) == "ahp-file:///node-a"


def test_a_path_outside_the_root_keeps_its_absolute_form_under_the_node() -> None:
    assert qualify_file_uris("file:///home/dev/.claude/plan.md", "node-a", ROOT) == (
        "ahp-file://node-a/home/dev/.claude/plan.md"
    )
    # A sibling that merely shares a prefix is outside, not inside.
    assert qualify_file_uris("file:///home/dev/src2/x", "node-a", ROOT) == (
        "ahp-file://node-a/home/dev/src2/x"
    )


def test_a_node_without_a_root_puts_everything_in_the_tree() -> None:
    assert qualify_file_uris("file:///srv/repo", "n1") == "ahp-file:///n1/srv/repo"


def test_windows_roots_match_whatever_the_drive_letter_case() -> None:
    root = root_of("file:///C:/Users/me/Github")
    assert root == "C:/Users/me/Github"
    assert qualify_file_uris("file:///c%3A/Users/me/Github/proj", "box", root) == (
        "ahp-file:///box/proj"
    )
    assert unqualify_file_uris("ahp-file:///box/proj", "box", root) == (
        "file:///C:/Users/me/Github/proj"
    )


def test_qualification_reaches_every_nested_string() -> None:
    payload = {"summary": {"workingDirectories": ["file:///srv", "file:///tmp"]}, "n": 3}
    assert qualify_file_uris(payload, "n1") == {
        "summary": {"workingDirectories": ["ahp-file:///n1/srv", "ahp-file:///n1/tmp"]},
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


@pytest.mark.parametrize("root", [None, ROOT])
def test_unqualify_is_the_inverse(root: str | None) -> None:
    original = {
        "uri": "file:///home/dev/src/a/b",
        "list": ["file:///home/dev/src", "file:///etc/hosts"],
    }
    assert unqualify_file_uris(qualify_file_uris(original, "n1", root), "n1", root) == original


def test_a_file_on_another_node_is_refused_not_forwarded() -> None:
    with pytest.raises(ForeignUriError):
        unqualify_file_uris({"uri": "ahp-file:///node-b/etc/passwd"}, "node-a")
    with pytest.raises(ForeignUriError):
        unqualify_file_uris({"uri": "ahp-file://node-b/etc/passwd"}, "node-a")


def test_climbing_out_of_the_root_is_refused() -> None:
    with pytest.raises(ForeignUriError, match="climbs out"):
        unqualify_file_uris("ahp-file:///node-a/../../etc/passwd", "node-a", ROOT)
    with pytest.raises(ForeignUriError, match="climbs out"):
        unqualify_file_uris("ahp-file:///node-a/x/%2E%2E/%2E%2E/y", "node-a", ROOT)


def test_the_virtual_root_is_not_a_file_on_any_node() -> None:
    with pytest.raises(ForeignUriError, match="list of nodes"):
        unqualify_file_uris(VIRTUAL_ROOT, "node-a", ROOT)


def test_a_clients_own_file_uri_passes_through() -> None:
    # `file:` is always the sender's own machine; the broker never claims it.
    assert unqualify_file_uris("file:///tmp", "node-a", ROOT) == "file:///tmp"


def test_node_of_and_the_virtual_root() -> None:
    assert node_of("ahp-file:///node-a/x") == "node-a"
    assert node_of("ahp-file:///node-a") == "node-a"
    assert node_of("ahp-file://node-a/abs/x") == "node-a"
    assert node_of(VIRTUAL_ROOT) is None
    assert node_of("file:///x") is None
    assert node_of("file://node-a/x") is None
    assert node_of(3) is None
    assert is_virtual_root("ahp-file:///")
    assert is_virtual_root("ahp-file://")
    assert not is_virtual_root("ahp-file:///node-a")


def test_root_of() -> None:
    assert root_of("file:///home/dev/src/") == "home/dev/src"
    assert root_of("file:///Users/me/My%20Code") == "Users/me/My Code"
    assert root_of(None) is None
    assert root_of("file:///") is None


def test_owned_channels_are_learned_from_any_depth() -> None:
    state = {
        "resource": "echo:/s1",
        "chats": [{"resource": "ahp-chat://c1"}, {"resource": "ahp-chat://c2"}],
        "annotations": {"resource": "echo:/s1/annotations"},
        "cwd": {"resource": "ahp-file:///n1/tmp"},
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
