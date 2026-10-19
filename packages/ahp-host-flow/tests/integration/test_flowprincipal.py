"""The run's principal reaches a custom verb, and `confirm` says who answered (M13)."""

from __future__ import annotations

from pathlib import Path

from ahp_host import LoopbackSingleUserPolicy
from ahp_host.core.policies import principal_from_header

from tests.flow_hosting import SESSION, boot_flow, final_json
from tests.hosting import connect, start_turn, wait_turn
from tests.integration.test_flowext_hostgates import decide, needed, py, seen

TOML = (
    '[providers.rec]\nload = "tests.flow_fakes:RecordingProvider"\n\n'
    "[verbs]\n"
    'who = "tests.hook_fakes:principal_verb"\n'
    'approve_person = "tests.hook_fakes:approve_person_verb"\n'
)

WHO_PY = (
    "meta = {'name': 'whoflow', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await who()\n"
)
PERSON_PY = (
    "meta = {'name': 'personflow', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await approve_person('alice')\n"
)


class HeaderPolicy(LoopbackSingleUserPolicy):
    principal_of = staticmethod(principal_from_header("x-forwarded-user"))


async def _who(tmp_path: Path, **boot: object) -> object:
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML, **boot)  # type: ignore[arg-type]
    try:
        await start_turn(fx.wire, fx.chat, "t1", py(tmp_path, "whoflow", WHO_PY))
        return final_json(await wait_turn(fx.wire, fx.chat, "t1"))
    finally:
        await fx.shut()


async def test_flowprincipal_verb_sees_the_turn_principal(tmp_path: Path) -> None:
    named = await _who(tmp_path, policy=HeaderPolicy(), headers={"x-forwarded-user": "alice"})
    assert named == "alice"
    assert await _who(tmp_path) is None


async def test_flowprincipal_approve_person_reasks(tmp_path: Path) -> None:
    fx = await boot_flow(
        tmp_path,
        str(tmp_path / "no-node"),
        TOML,
        policy=HeaderPolicy(),
        headers={"x-forwarded-user": "alice"},
    )
    bob, bob_serving = await connect(fx.host, headers={"x-forwarded-user": "bob"}, client_id="c2")
    try:
        await bob.request("subscribe", {"channel": SESSION})
        await bob.request("subscribe", {"channel": fx.chat})
        await start_turn(fx.wire, fx.chat, "t1", py(tmp_path, "personflow", PERSON_PY))
        known: set[str] = set()
        # Bob approves the first confirmation: it is not Alice's, so the verb asks again.
        assert await bob.until(seen(fx, known)), needed(fx)
        first = next(e for e in needed(fx) if e["id"] not in known)
        known.add(str(first["id"]))
        await bob.dispatch(
            fx.chat,
            {
                "type": "chat/toolCallConfirmed",
                "turnId": first["turnId"],
                "toolCallId": first["toolCall"]["toolCallId"],
                "approved": True,
                "confirmed": "user-action",
            },
        )
        await decide(fx, known, approved=True)
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == {"approved": True, "answeredBy": ["bob", "alice"]}
    finally:
        await bob.close()
        bob_serving.cancel()
        await fx.shut()


async def test_flowprincipal_is_never_the_session_creators(tmp_path: Path) -> None:
    """A turn without a principal runs as nobody, not as the alice who created the session."""
    fx = await boot_flow(
        tmp_path,
        str(tmp_path / "no-node"),
        TOML,
        policy=HeaderPolicy(),
        headers={"x-forwarded-user": "alice"},
    )
    anon, anon_serving = await connect(fx.host, client_id="c2")
    try:
        await anon.request("subscribe", {"channel": SESSION})
        await anon.request("subscribe", {"channel": fx.chat})
        # Alice's own turn proves the session was created by her ...
        await start_turn(fx.wire, fx.chat, "t1", py(tmp_path, "whoflow", WHO_PY))
        assert final_json(await wait_turn(fx.wire, fx.chat, "t1")) == "alice"
        # ... and a turn from a connection with no principal does not inherit it.
        await start_turn(anon, fx.chat, "t2", "/whoflow")
        assert final_json(await wait_turn(anon, fx.chat, "t2")) is None
    finally:
        await anon.close()
        anon_serving.cancel()
        await fx.shut()
