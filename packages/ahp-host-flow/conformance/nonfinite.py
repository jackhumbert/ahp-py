import math
from typing import Any

from ahp_host_flow.verbs import agent, budget

meta = {"name": "nonfinite", "description": "Infinity and NaN cross as null"}


async def main(args: Any) -> Any:
    await agent("a", temperature=math.nan)
    return {"left": budget.remaining(), "nan": math.nan, "nested": [-math.inf, 1.5]}
