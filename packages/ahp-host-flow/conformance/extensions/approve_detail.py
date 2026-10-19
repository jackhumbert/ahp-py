from typing import Any

from ahp_host_flow.verbs import approve

meta = {
    "name": "approve_detail",
    "description": "approve() with x.detail gives {approved, reason, answeredBy}",
}


async def main(args: Any) -> Any:
    plain = await approve("Ship?")
    rich = await approve("Drop?", x={"detail": True})
    ok = await approve("Ship?", x={"detail": True})
    bad = None
    try:
        await approve("Bad?", x=1)  # type: ignore[arg-type]
    except TypeError as e:
        bad = str(e)
    return {"plain": plain, "rich": rich, "ok": ok, "bad": bad}
