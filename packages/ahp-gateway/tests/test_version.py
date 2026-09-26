"""The version is written once and reads back."""

import importlib.metadata

import ahp_gateway


def test_version_is_a_string() -> None:
    assert isinstance(ahp_gateway.__version__, str)
    assert ahp_gateway.__version__


def test_version_is_semver_shaped() -> None:
    # `0.1.0.dev0` at scaffold time; a release tag must equal it (RELEASING).
    parts = ahp_gateway.__version__.split(".")
    assert len(parts) >= 3
    assert parts[0].isdigit()
    assert parts[1].isdigit()


def test_installed_version_matches_the_source() -> None:
    # Only meaningful from an install (CI's smoke job); from a checkout the
    # distribution is absent and the assertion is skipped, not relaxed.
    try:
        installed = importlib.metadata.version("ahp-gateway")
    except importlib.metadata.PackageNotFoundError:
        import pytest

        pytest.skip("package not installed")
    assert installed == ahp_gateway.__version__
