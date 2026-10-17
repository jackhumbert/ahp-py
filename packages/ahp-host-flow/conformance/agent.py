from typing import Any

from ahp_host_flow.verbs import agent

meta = {"name": "agent", "description": "agent() returns text, or null on skip or error"}


async def main(args: Any) -> Any:
    text = await agent("hello", label="greet", x={"provider": "any"}, unknownKey=1)
    skipped = await agent("skip me")
    failed = await agent("fail me")
    return {"text": text, "skipped": skipped, "failed": failed}
