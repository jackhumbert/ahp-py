from typing import Any

from ahp_host_flow.verbs import agent

meta = {"name": "callcwd", "description": "opts.x.cwd reaches the dispatcher unchanged"}


async def main(args: Any) -> Any:
    a = await agent("a", x={"cwd": "sub"})
    b = await agent("b", x={"cwd": "/srv/project"})
    c = await agent("c")
    return [a, b, c]
