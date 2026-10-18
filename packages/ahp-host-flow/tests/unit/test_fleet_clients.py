"""`FleetClients`: which client offers the fleet tools, and whether it left meanwhile."""

from __future__ import annotations

import asyncio
from typing import Any

from ahp_host_flow.fleet import FLEET_TOOLS, FleetClients


def gateway(client_id: str) -> dict[str, Any]:
    return {"clientId": client_id, "tools": [{"name": n} for n in FLEET_TOOLS]}


async def test_fleet_clients_a_departure_is_seen_even_when_the_same_id_comes_back() -> None:
    """A gateway rejoins under the same client id: only the generation shows it left."""
    clients = FleetClients([gateway("gw")])
    before = clients.generation
    waiter = asyncio.create_task(clients.departed_since(before))
    clients.update([])
    clients.update([gateway("gw")])
    await asyncio.wait_for(waiter, 1)
    assert clients.current() == "gw"
    assert clients.generation == before + 1


async def test_fleet_clients_only_a_client_offering_every_tool_counts() -> None:
    clients = FleetClients([{"clientId": "editor", "tools": [{"name": "start_session"}]}])
    assert clients.current() is None
    assert await clients.wait(0.05) is None
    clients.update([{"clientId": "editor", "tools": []}, gateway("gw")])
    assert await clients.wait(0.05) == "gw"
    # Unchanged clients are no departure.
    generation = clients.generation
    clients.update([gateway("gw")])
    assert clients.generation == generation
