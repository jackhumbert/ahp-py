"""Changeset operation parameters carried in ``_meta``.

This is an ahp-py extension: a client puts the parameters of a changeset operation
under ``InvokeChangesetOperationParams._meta[OPERATION_META_KEY]``, which a stock host
ignores. Pure and stdlib-only. :func:`operation_params` never raises: the value is
data written by a peer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "OPERATION_META_KEY",
    "operation_meta",
    "operation_params",
]

#: Key in ``InvokeChangesetOperationParams._meta``; the value is the operation's parameters.
OPERATION_META_KEY: Final = "ahp-py/operation"


def operation_meta(params: Mapping[str, Any]) -> dict[str, Any]:
    """The ``_meta`` object that carries ``params``."""
    return {OPERATION_META_KEY: dict(params)}


def operation_params(meta: Any) -> dict[str, Any]:
    """The parameters in ``meta``; ``{}`` when absent or not a mapping."""
    if isinstance(meta, Mapping):
        value = meta.get(OPERATION_META_KEY)
        if isinstance(value, Mapping):
            return dict(value)
    return {}
