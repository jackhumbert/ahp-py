from typing import Any

from ahp_host_flow.verbs import approve, ask, canvas

meta = {
    "name": "no_gates",
    "description": "a run with no answer path fails ask() and approve() clearly",
}


async def main(args: Any) -> Any:
    out = []
    try:
        await ask("q")
    except Exception as e:
        out.append(str(e))
    try:
        await approve("s")
    except Exception as e:
        out.append(str(e))
    canvas("https://example.com/x")
    return out
