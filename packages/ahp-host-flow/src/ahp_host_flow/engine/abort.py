"""A minimal abort signal: one-shot, with ordered callbacks."""

from __future__ import annotations

from collections.abc import Callable


class AbortSignal:
    def __init__(self) -> None:
        self._aborted = False
        self._callbacks: list[Callable[[], None]] = []

    @property
    def aborted(self) -> bool:
        return self._aborted

    def abort(self) -> None:
        """Set the signal and call each subscriber once; later calls do nothing."""
        if self._aborted:
            return
        self._aborted = True
        for callback in list(self._callbacks):
            callback()

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call `callback` on abort (now, if already aborted); returns an unsubscribe."""
        if self._aborted:
            callback()
            return lambda: None
        self._callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

        return unsubscribe
