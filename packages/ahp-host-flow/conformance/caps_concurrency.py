from typing import Any

from ahp_host_flow.verbs import agent, parallel

meta = {
    "name": "caps-concurrency",
    "description": "at most the configured number of agents run at once",
}


async def main(args: Any) -> Any:
    return await parallel([lambda i=i: agent(f"c{i}") for i in range(12)])  # type: ignore[misc]
