"""The version is written once and reads back."""

import importlib.metadata

import agent_host_broker


def test_version_is_a_string() -> None:
    assert isinstance(agent_host_broker.__version__, str)
    assert agent_host_broker.__version__


def test_version_is_semver_shaped() -> None:
    # `0.1.0.dev0` at scaffold time; a release tag must equal it (RELEASING).
    parts = agent_host_broker.__version__.split(".")
    assert len(parts) >= 3
    assert parts[0].isdigit()
    assert parts[1].isdigit()


def test_installed_version_matches_the_source() -> None:
    # Only meaningful from an install (CI's smoke job); from a checkout the
    # distribution is absent and the assertion is skipped, not relaxed.
    try:
        installed = importlib.metadata.version("agent-host-broker")
    except importlib.metadata.PackageNotFoundError:
        import pytest

        pytest.skip("package not installed")
    assert installed == agent_host_broker.__version__
