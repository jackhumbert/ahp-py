import datetime
import random
import time
from collections.abc import Callable
from typing import Any

meta = {
    "name": "determinism",
    "description": "non-deterministic APIs throw; explicit dates work",
}


async def main(args: Any) -> Any:
    # Python cannot ban these (plan section 4.10); the JS twin asserts they throw.
    tries: dict[str, Callable[[], Any]] = {
        "time.time()": time.time,
        "random.random()": random.random,
        "datetime.now()": datetime.datetime.now,
    }
    banned = {}
    for k, f in tries.items():
        try:
            f()
            banned[k] = False
        except Exception as e:
            banned[k] = "is not allowed in a workflow script" in str(e)
    return {
        "banned": banned,
        "explicit": datetime.datetime.fromtimestamp(0, datetime.UTC).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        ),
        "apis": [
            "undefined" if n not in globals() else "defined"
            for n in ("require", "process", "console", "setTimeout")
        ],
    }
