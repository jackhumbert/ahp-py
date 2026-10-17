from typing import Any

from ahp_host_flow.verbs import agent, workflow

meta = {
    "name": "workflow",
    "description": "workflow() runs a child inline, sharing caps and budget; one level only",
}


async def main(args: Any) -> Any:
    # 'child' resolves to child.js (JS wins the tie), so this is a Python parent with a JS child.
    by_name = await workflow("child", {"who": "name"})
    by_path = await workflow({"scriptPath": "workflows/child.py"}, {"who": "path"})
    nested = None
    try:
        await workflow("nested")
    except Exception as e:
        nested = str(e)
    unknown = None
    try:
        await workflow("no-such-workflow")
    except Exception as e:
        unknown = str(e)
    broken = None
    try:
        await workflow("broken")
    except Exception as e:
        broken = str(e).startswith("workflow ")
    after = await agent("parent")
    return {
        "byName": by_name,
        "byPath": by_path,
        "nested": nested,
        "unknown": unknown,
        "broken": broken,
        "after": after,
        "args": "undefined" if args is None else type(args).__name__,
    }
