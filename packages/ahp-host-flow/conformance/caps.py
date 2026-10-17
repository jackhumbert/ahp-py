from typing import Any

from ahp_host_flow.verbs import agent, parallel

meta = {"name": "caps", "description": "1000 agent() calls per run; 4096 items per call"}


async def main(args: Any) -> Any:
    many = await parallel([lambda i=i: agent(f"a{i}") for i in range(1000)])  # type: ignore[misc]
    over = None
    try:
        await agent("one too many")
    except Exception as e:
        over = str(e)
    items = await parallel([lambda: 1] * 4096)
    return {
        "ok": sum(1 for i, r in enumerate(many) if r == f"a{i}"),
        "over": over,
        "items": len(items),
    }
