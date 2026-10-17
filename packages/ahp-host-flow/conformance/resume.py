from typing import Any

from ahp_host_flow.verbs import agent, parallel, phase, pipeline

meta = {
    "name": "resume",
    "description": (
        "a resumed run replays completed agent() calls from the journal and runs the rest live"
    ),
}


async def main(args: Any) -> Any:
    phase("Plan")
    plan = await agent("plan")
    x, y = await parallel([lambda: agent("x", label="x"), lambda: agent("y", label="y")])
    phase("Build")
    built = await pipeline(["p", "q"], lambda prev, item, index: agent("hang build " + item))
    return {"plan": plan, "x": x, "y": y, "built": built}
