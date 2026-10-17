"""The dispatcher interface and the echo dispatcher."""

from __future__ import annotations

from ahp_host_flow.engine import AgentCall, Dispatcher, EchoDispatcher


async def test_dispatcher_echo_returns_prompt() -> None:
    result = await EchoDispatcher().run_agent(AgentCall(ordinal=0, prompt="hi"))
    assert result.value == "hi"
    assert result.usage == {}


async def test_dispatcher_echo_records_calls() -> None:
    dispatcher = EchoDispatcher()
    first = AgentCall(ordinal=0, prompt="a", opts={"model": "m", "extra": 1})
    second = AgentCall(ordinal=1, prompt="b")
    await dispatcher.run_agent(first)
    await dispatcher.run_agent(second)
    assert dispatcher.calls == [first, second]
    assert [c.ordinal for c in dispatcher.calls] == [0, 1]
    assert dispatcher.calls[0].opts == {"model": "m", "extra": 1}
    assert dispatcher.calls[1].opts == {}


def test_dispatcher_echo_is_a_dispatcher() -> None:
    assert isinstance(EchoDispatcher(), Dispatcher)
