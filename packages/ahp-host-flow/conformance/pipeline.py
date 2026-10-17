from typing import Any

from ahp_host_flow.verbs import agent, pipeline

meta = {
    "name": "pipeline",
    "description": (
        "pipeline() has no barrier; a stage gets (prev, item, index); a throw drops the item"
    ),
}


async def main(args: Any) -> Any:
    seen: list[list[Any]] = []

    async def first(prev: Any, item: Any, index: int) -> Any:
        seen.append(["s1", prev, item, index])
        if item == "y":
            raise Exception("drop")
        return await agent(prev + "1")

    async def second(prev: Any, item: Any, index: int) -> Any:
        seen.append(["s2", prev, item, index])
        return prev + "2"

    out = await pipeline(["x", "y", "z"], first, second)
    s1 = sorted((s for s in seen if s[0] == "s1"), key=lambda s: s[3])
    s2 = sorted((s for s in seen if s[0] == "s2"), key=lambda s: s[3])
    rejected = None
    try:
        await pipeline([0] * 4097, lambda prev, item, index: prev)
    except Exception as e:
        rejected = str(e)
    passthrough = await pipeline([1, 2])
    return {"out": out, "s1": s1, "s2": s2, "rejected": rejected, "passthrough": passthrough}
