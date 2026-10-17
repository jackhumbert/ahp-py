from typing import Any

from ahp_host_flow.verbs import agent, log, parallel, phase

meta = {
    "name": "phase-log-args",
    "description": "phase() groups calls, log() narrates, args pass verbatim",
    "phases": [{"title": "Map"}, {"title": "Reduce"}],
}


async def main(args: Any) -> Any:
    phase("Map")
    log(f"mapping {len(args['items'])} items")
    mapped = await parallel(
        [lambda item=item: agent(item, label=f"map {item}") for item in args["items"]]  # type: ignore[misc]
    )
    phase("Reduce")
    reduced = await agent("+".join(mapped), phase="Custom", label="reduce")
    return {"args": args, "reduced": reduced}
