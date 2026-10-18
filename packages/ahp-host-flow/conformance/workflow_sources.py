from typing import Any

from ahp_host_flow.verbs import workflow

meta = {
    "name": "workflow_sources",
    "description": "workflow() takes a source-qualified name",
}


async def main(args: Any) -> Any:
    host = await workflow("host:child", {"who": "host"})
    workspace = None
    try:
        await workflow("workspace:child")
    except Exception as e:
        workspace = str(e)
    bogus = None
    try:
        await workflow("bogus:child")
    except Exception as e:
        bogus = str(e)
    return {"host": host, "workspace": workspace, "bogus": bogus}
