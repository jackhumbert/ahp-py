"""`python -m ahp_host_flow`: building the host and reporting config errors."""

from pathlib import Path
from typing import Any

import pytest
from ahp_host.core.store import FileSessionStore

from ahp_host_flow.__main__ import build_host, main
from ahp_host_flow.config import load_config

ECHO_TOML = """\
state_dir = "state"

[providers.echo]
load = "ahp_host.provider.echo:EchoProvider"
"""


def test_build_host_serves_the_flow_provider(tmp_path: Path) -> None:
    toml = tmp_path / "flow.toml"
    toml.write_text(ECHO_TOML, encoding="utf-8")
    host = build_host(load_config(toml))
    assert host.provider.agent.provider == "flow"


def test_missing_config_exits(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--config", str(tmp_path / "missing.toml")])
    assert "no config file" in str(info.value.code)


def test_unknown_key_exits(tmp_path: Path) -> None:
    toml = tmp_path / "flow.toml"
    toml.write_text("bogus = 1\n" + ECHO_TOML, encoding="utf-8")
    with pytest.raises(SystemExit) as info:
        main(["--config", str(toml)])
    assert "unknown" in str(info.value.code)


def test_a_broken_provider_does_not_stop_startup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    toml = tmp_path / "flow.toml"
    toml.write_text(
        ECHO_TOML
        + '\n[providers.gone]\nload = "tests.flow_nope:Missing"\n\n[routing]\ndefault = "echo"\n',
        encoding="utf-8",
    )
    host = build_host(load_config(toml))
    with caplog.at_level("WARNING"):
        models = host.provider.agent.models
    assert models  # the echo provider's, with the broken one omitted
    assert any("provider 'gone' failed to load" in r.getMessage() for r in caplog.records)
    assert list(host.provider.providers) == ["echo", "gone"]  # type: ignore[attr-defined]


def test_non_loopback_bind_needs_a_token_file(tmp_path: Path) -> None:
    toml = tmp_path / "flow.toml"
    toml.write_text(ECHO_TOML, encoding="utf-8")
    with pytest.raises(SystemExit) as info:
        main(["--config", str(toml), "--bind", "0.0.0.0"])
    assert "--token-file" in str(info.value.code)


@pytest.mark.parametrize("bind", ["127.0.0.1", "::1", "localhost"])
def test_loopback_bind_needs_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bind: str
) -> None:
    served: list[tuple[str, str | None]] = []

    async def fake_serve(host: object, *, bind: str, port: int, token: str | None) -> None:
        served.append((bind, token))

    monkeypatch.setattr("ahp_host_flow.__main__._serve", fake_serve)
    toml = tmp_path / "flow.toml"
    toml.write_text(ECHO_TOML, encoding="utf-8")
    main(["--config", str(toml), "--bind", bind])
    assert served == [(bind, None)]


def test_non_loopback_bind_with_a_token_file_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served: list[tuple[str, str | None]] = []

    async def fake_serve(host: object, *, bind: str, port: int, token: str | None) -> None:
        served.append((bind, token))

    monkeypatch.setattr("ahp_host_flow.__main__._serve", fake_serve)
    toml = tmp_path / "flow.toml"
    toml.write_text(ECHO_TOML, encoding="utf-8")
    token = tmp_path / "token"
    token.write_text("s3cret\n", encoding="utf-8")
    main(["--config", str(toml), "--bind", "0.0.0.0", "--token-file", str(token)])
    assert served == [("0.0.0.0", "s3cret")]


def test_flow_main_root_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hosts: list[Any] = []

    async def fake_serve(host: Any, *, bind: str, port: int, token: str | None) -> None:
        hosts.append(host)

    monkeypatch.setattr("ahp_host_flow.__main__._serve", fake_serve)
    toml = tmp_path / "flow.toml"
    toml.write_text(ECHO_TOML, encoding="utf-8")
    a, b = tmp_path / "a", tmp_path / "b"
    main(["--config", str(toml), "--root", str(a), "--root", str(b)])
    (host,) = hosts
    assert host.provider.roots.paths == (a.resolve(), b.resolve())


def test_flow_main_store_is_a_file_store_under_the_state_dir(tmp_path: Path) -> None:
    toml = tmp_path / "flow.toml"
    toml.write_text(ECHO_TOML, encoding="utf-8")
    config = load_config(toml)
    host = build_host(config)
    assert isinstance(host.store, FileSessionStore)
    assert host.store.directory == config.state_dir / "sessions"


def test_flow_main_store_no_store_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hosts: list[Any] = []

    async def fake_serve(host: Any, *, bind: str, port: int, token: str | None) -> None:
        hosts.append(host)

    monkeypatch.setattr("ahp_host_flow.__main__._serve", fake_serve)
    toml = tmp_path / "flow.toml"
    toml.write_text(ECHO_TOML, encoding="utf-8")
    main(["--config", str(toml), "--no-store"])
    (host,) = hosts
    assert not isinstance(host.store, FileSessionStore)
