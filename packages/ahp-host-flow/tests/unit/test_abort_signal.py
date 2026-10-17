"""Abort signal."""

from ahp_host_flow.engine import AbortSignal


def test_abort_signal_calls_callbacks_once() -> None:
    signal = AbortSignal()
    calls: list[str] = []
    signal.subscribe(lambda: calls.append("a"))
    signal.subscribe(lambda: calls.append("b"))
    assert not signal.aborted
    signal.abort()
    signal.abort()
    assert signal.aborted
    assert calls == ["a", "b"]


def test_abort_signal_subscribe_after_abort_calls_now() -> None:
    signal = AbortSignal()
    signal.abort()
    calls: list[str] = []
    unsubscribe = signal.subscribe(lambda: calls.append("x"))
    assert calls == ["x"]
    unsubscribe()


def test_abort_signal_unsubscribe() -> None:
    signal = AbortSignal()
    calls: list[str] = []
    unsubscribe = signal.subscribe(lambda: calls.append("x"))
    unsubscribe()
    unsubscribe()
    signal.abort()
    assert calls == []
