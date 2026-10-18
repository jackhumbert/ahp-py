from typing import Any

from ahp_host_flow.verbs import agent, verb

meta = {"name": "verbs", "description": "custom verbs reach host handlers without a model"}


def kind(name: str) -> str:
    return "function" if callable(globals().get(name)) else "undefined"


async def main(args: Any) -> Any:
    found = await verb("tool", "search", {"q": "docs"})
    view = await verb("render", {"title": "T"})
    ok = await verb("approve", "ada", "Ship it?")
    caught = None
    try:
        await verb("boom", 1)
    except Exception as e:
        caught = str(e)
    kinds = [kind("tool"), kind("nope"), kind("ask")]
    a = await agent("after")
    return {"found": found, "view": view, "ok": ok, "caught": caught, "kinds": kinds, "a": a}
