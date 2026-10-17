from typing import Any

from ahp_host_flow.verbs import agent

meta = {"name": "agent_schema", "description": "agent() re-validates a result against opts.schema"}


async def main(args: Any) -> Any:
    ok = await agent("fine", schema={"type": "string"})
    bad = await agent("not an object", schema={"type": "object"})
    ignored = await agent("no schema object", schema="not a schema")
    return {"ok": ok, "bad": bad, "ignored": ignored}
