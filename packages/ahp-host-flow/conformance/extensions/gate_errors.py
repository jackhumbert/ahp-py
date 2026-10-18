from typing import Any

from ahp_host_flow.verbs import approve, ask, canvas

meta = {
    "name": "gate_errors",
    "description": "bad ask(), approve() and canvas() arguments throw before the wire",
}


async def main(args: Any) -> Any:
    out = []
    try:
        await ask(1)
    except Exception as e:
        out.append(str(e))
    try:
        await ask("q", choices=[])
    except Exception as e:
        out.append(str(e))
    try:
        await ask("q", choices=["a"], schema={})
    except Exception as e:
        out.append(str(e))
    try:
        await ask("q", schema=5)
    except Exception as e:
        out.append(str(e))
    try:
        await approve(2)
    except Exception as e:
        out.append(str(e))
    try:
        await approve("s", diff=5)
    except Exception as e:
        out.append(str(e))
    try:
        canvas("ftp://x")
    except Exception as e:
        out.append(str(e))
    try:
        canvas("https://x", title=3)
    except Exception as e:
        out.append(str(e))
    return out
