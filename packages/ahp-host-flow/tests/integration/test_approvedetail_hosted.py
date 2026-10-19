"""`approve()` with `x.detail` on the run's main chat: the reason and who answered (plan §8 M15)."""

from __future__ import annotations

from pathlib import Path

from tests.flow_hosting import SESSION, boot_flow, final_json
from tests.hosting import start_turn, state, wait_turn
from tests.integration.test_flowext_hostgates import needed, py, seen
from tests.integration.test_flowprincipal import HeaderPolicy

TOML = '[providers.rec]\nload = "tests.flow_fakes:RecordingProvider"\n'
DETAIL_PY = (
    "meta = {'name': 'detailflow', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await approve('Drop?', x={'detail': True})\n"
)


async def test_approvedetail_hosted_reason(tmp_path: Path) -> None:
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        await start_turn(fx.wire, fx.chat, "t1", py(tmp_path, "detailflow", DETAIL_PY))
        assert await fx.wire.until(seen(fx, set())), state(fx.host, SESSION)
        entry = needed(fx)[0]
        await fx.wire.dispatch(
            fx.chat,
            {
                "type": "chat/toolCallConfirmed",
                "turnId": entry["turnId"],
                "toolCallId": entry["toolCall"]["toolCallId"],
                "approved": False,
                "reason": "denied",
                "reasonMessage": "too risky",
                "confirmed": "user-action",
            },
        )
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == {"approved": False, "reason": "too risky", "answeredBy": None}
    finally:
        await fx.shut()


async def test_approvedetail_hosted_answered_by(tmp_path: Path) -> None:
    fx = await boot_flow(
        tmp_path,
        str(tmp_path / "no-node"),
        TOML,
        policy=HeaderPolicy(),
        headers={"x-forwarded-user": "alice"},
    )
    try:
        await start_turn(fx.wire, fx.chat, "t1", py(tmp_path, "detailflow", DETAIL_PY))
        assert await fx.wire.until(seen(fx, set())), state(fx.host, SESSION)
        entry = needed(fx)[0]
        await fx.wire.dispatch(
            fx.chat,
            {
                "type": "chat/toolCallConfirmed",
                "turnId": entry["turnId"],
                "toolCallId": entry["toolCall"]["toolCallId"],
                "approved": True,
                "confirmed": "user-action",
            },
        )
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == {"approved": True, "reason": None, "answeredBy": "alice"}
    finally:
        await fx.shut()
