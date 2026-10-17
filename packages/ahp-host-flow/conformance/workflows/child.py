from typing import Any

from ahp_host_flow.verbs import agent

meta = {"name": "child", "description": "a child workflow"}


async def main(args: Any) -> Any:
    return {"reply": await agent(f"child {args['who']}"), "sawArgs": args}
