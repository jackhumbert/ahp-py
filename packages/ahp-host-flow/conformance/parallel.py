from typing import Any

from ahp_host_flow.verbs import agent, parallel

meta = {
    "name": "parallel",
    "description": "parallel() is a barrier; a throwing thunk resolves to null",
}


async def main(args: Any) -> Any:
    order: list[str] = []

    # Slow: finishes several awaits after the sync thunk, and must still finish before
    # parallel returns.
    async def slow() -> Any:
        r = await agent("a")
        await agent("a2")
        await agent("a3")
        order.append("a")
        return r

    def sync_throw() -> Any:
        raise Exception("sync throw")

    async def async_throw() -> Any:
        raise Exception("async throw")

    def sync_value() -> Any:
        order.append("sync")
        return 42

    results = await parallel([slow, sync_throw, async_throw, lambda: agent("fail b"), sync_value])
    order.append("after")
    rejected = None
    try:
        await parallel([lambda: 1] * 4097)
    except Exception as e:
        rejected = str(e)
    empty = await parallel([])
    return {"results": results, "order": order, "rejected": rejected, "empty": empty}
