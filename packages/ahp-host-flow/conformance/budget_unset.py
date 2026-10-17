import math
from typing import Any

from ahp_host_flow.verbs import agent, budget

meta = {"name": "budget-unset", "description": "without a total the budget is unlimited"}


async def main(args: Any) -> Any:
    await agent("a")
    return [budget.total, budget.spent(), budget.remaining() == math.inf]
