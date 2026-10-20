"""`[limits]` on the provider path: budget and concurrency from flow.toml (M16)."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from ahp_host import AgentSessionContext, TurnSink, UserMessage

from tests.flow_hosting import boot_flow, final_json
from tests.hosting import run_turn
from tests.limits_fakes import ScriptedProvider, ScriptedSession

TOML = '[providers.scripted]\nload = "tests.limits_fakes:ScriptedProvider"\n'
COUNTING_TOML = (
    '[providers.counting]\nload = "tests.integration.test_flowlimits_hosted:CountingProvider"\n'
)


class CountingSession(ScriptedSession):
    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        CountingProvider.in_flight += 1
        CountingProvider.max_in_flight = max(
            CountingProvider.max_in_flight, CountingProvider.in_flight
        )
        try:
            await super().send_user_message(message, sink)
        finally:
            CountingProvider.in_flight -= 1


class CountingProvider(ScriptedProvider):
    """Scripted sessions that count how many turns run at once."""

    in_flight: ClassVar[int] = 0
    max_in_flight: ClassVar[int] = 0

    async def create_session(self, context: AgentSessionContext) -> CountingSession:
        session = CountingSession(context)
        self.sessions.append(session)
        return session


def write_workflow(base: Path, source: str) -> None:
    user = base / "user"
    user.mkdir()
    (user / "lim.py").write_text(source, encoding="utf-8")


BUDGET_WORKFLOW = (
    "meta = {'name': 'lim', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    first = await agent('one')\n"
    "    try:\n"
    "        second = await agent('two')\n"
    "    except Exception as exc:\n"
    "        second = str(exc)\n"
    "    return {'first': first, 'second': second}\n"
)

PARALLEL_WORKFLOW = (
    "meta = {'name': 'lim', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await parallel([\n"
    "        lambda: agent('requests:1:0:0.1'),\n"
    "        lambda: agent('requests:1:0:0.12'),\n"
    "    ])\n"
)


async def test_flowlimits_hosted_budget(tmp_path: Path) -> None:
    write_workflow(tmp_path, BUDGET_WORKFLOW)
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML + "[limits]\nbudget = 1\n")
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "/lim")
    finally:
        await fx.shut()
    value = final_json(turn)
    assert value["first"] == "ok: one"
    assert "budget exhausted" in value["second"]


async def test_flowlimits_hosted_concurrency(tmp_path: Path) -> None:
    CountingProvider.in_flight = CountingProvider.max_in_flight = 0
    write_workflow(tmp_path, PARALLEL_WORKFLOW)
    fx = await boot_flow(
        tmp_path, str(tmp_path / "no-node"), COUNTING_TOML + "[limits]\nconcurrency = 1\n"
    )
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "/lim")
    finally:
        await fx.shut()
    assert final_json(turn) == ["ok", "ok"]
    assert CountingProvider.max_in_flight == 1


NAMED_COST_WORKFLOW = (
    "meta = {'name': 'lim', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    first = await agent('acp-cost:0.3', x={'chat': 'c'})\n"
    "    second = await agent('acp-cost:0.3', x={'chat': 'c'})\n"
    "    return [first, second]\n"
)


async def test_flowlimits_hosted_acp_cost_is_a_delta_per_call(tmp_path: Path) -> None:
    # One session (a named chat) reports running totals 0.3 then 0.6: the calls cost 0.3 each,
    # 0.6 in all, within the 0.7 budget (summing the totals, 0.9, would stop the second).
    write_workflow(tmp_path, NAMED_COST_WORKFLOW)
    fx = await boot_flow(
        tmp_path, str(tmp_path / "no-node"), TOML + "[limits]\ncost_budget = 0.7\n"
    )
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "/lim")
    finally:
        await fx.shut()
    assert final_json(turn) == ["ok", "ok"]
