"""The session's client tools, filtered for workers (plan §8 M17 item 1)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class ClientTools:
    """`activeClients` of the session, cut down by `[workflows] client_tools`.

    The gateway's fleet tools are never offered, whatever the setting: no tool of the client
    that offers them (the gateway may offer more than the four), and no tool of another client
    under one of their names.
    """

    def __init__(
        self, setting: bool | tuple[str, ...], clients: Sequence[Mapping[str, Any]] = ()
    ) -> None:
        self._setting = setting
        self._clients: list[Mapping[str, Any]] = []
        self.update(clients)

    def update(self, clients: Sequence[Mapping[str, Any]]) -> None:
        self._clients = [
            c for c in clients if isinstance(c, Mapping) and isinstance(c.get("clientId"), str)
        ]

    def _allowed(self, name: str) -> bool:
        # Imported here: fleet imports dispatch, which imports this module.
        from ahp_host_flow.fleet import FLEET_TOOLS

        if self._setting is False or name in FLEET_TOOLS:
            return False
        if isinstance(self._setting, tuple):
            return name in self._setting
        return True

    def clients(self) -> list[dict[str, Any]]:
        # Imported here: fleet imports dispatch, which imports this module.
        from ahp_host_flow.fleet import offers_fleet

        out: list[dict[str, Any]] = []
        for client in self._clients:
            if offers_fleet(client):
                continue
            tools = [
                t
                for t in client.get("tools") or ()
                if isinstance(t, Mapping)
                and isinstance(t.get("name"), str)
                and self._allowed(t["name"])
            ]
            if tools:
                out.append({**client, "tools": tools})
        return out

    def first(self) -> tuple[str | None, tuple[Mapping[str, Any], ...]]:
        clients = self.clients()
        if not clients:
            return None, ()
        return clients[0]["clientId"], tuple(clients[0]["tools"])

    def definitions(self) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for client in self.clients():
            for tool in client["tools"]:
                if tool["name"] not in seen:
                    seen.add(tool["name"])
                    out.append(dict(tool))
        return out

    def offers(self, client_id: str, name: str) -> bool:
        """Whether *client_id*'s tool *name* is one the workers are offered."""
        return any(
            client["clientId"] == client_id and any(t["name"] == name for t in client["tools"])
            for client in self.clients()
        )

    def owner(self, name: str) -> str | None:
        for client in self.clients():
            if any(t["name"] == name for t in client["tools"]):
                return str(client["clientId"])
        return None
