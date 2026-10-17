from typing import Any

from ahp_host_flow.verbs import agent, budget

meta = {"name": "budget", "description": "budget is a hard ceiling summed from output tokens"}


async def main(args: Any) -> Any:
    before = [budget.total, budget.spent(), budget.remaining()]
    a = await agent("a")
    b = await agent("b")
    mid = [budget.spent(), budget.remaining()]
    c = await agent("c")
    thrown = None
    try:
        await agent("d")
    except Exception as e:
        thrown = str(e)
    return {
        "before": before,
        "mid": mid,
        "after": [budget.spent(), budget.remaining()],
        "results": [a, b, c],
        "thrown": thrown,
    }
