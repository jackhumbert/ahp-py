from typing import Any

from ahp_host_flow.verbs import agent, approve, ask, canvas

meta = {"name": "gates", "description": "ask(), approve() and canvas() reach the host as gates"}


async def main(args: Any) -> Any:
    a = await agent("first")
    yes = await ask("Ship?", choices=["yes", "no"])
    name = await ask("Name?")
    cfg = await ask(
        "Config?",
        schema={"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
    )
    bad = await ask("Pick?", choices=["a", "b"])
    ok = await approve("Delete?", diff="-x\n+y\n")
    no = await approve("Drop table?")
    canvas("https://example.com/view", title="View")
    b = await agent("second")
    return {"a": a, "yes": yes, "name": name, "cfg": cfg, "bad": bad, "ok": ok, "no": no, "b": b}
