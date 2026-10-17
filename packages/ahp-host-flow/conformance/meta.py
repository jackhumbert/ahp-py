from typing import Any

meta = {
    "name": "meta",
    "description": "meta is a pure literal read without running the script",
    "whenToUse": "conformance",
    "phases": [{"title": "One", "detail": "first", "model": "haiku"}],
    "x": {"ignored": True},
}


async def main(args: Any) -> Any:
    return meta["name"]
