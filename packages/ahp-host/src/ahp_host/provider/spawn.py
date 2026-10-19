"""Environment filtering for spawned agent processes (plan M14 item 2).

No spawn path filtered the environment before this: a child inherited every
variable of the host, API keys and tokens included. `child_env` builds the
environment a provider hands to its child when the provider's `env_allow` is
set. The `ahp-host` and `ahp-host-claude` CLIs keep inheriting by default;
nothing here changes them.

Standard library only (`provider/` may import nothing else but the protocol
package).
"""

from __future__ import annotations

import fnmatch
import os
import sys
from collections.abc import Iterable, Mapping
from typing import Final

SECRET_WORDS: Final = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "PASSPHRASE",
    "CREDENTIAL",
    "API_KEY",
    "APIKEY",
    "PRIVATE_KEY",
    "ACCESS_KEY",
    "AUTH",
    "COOKIE",
)
_GLOB_CHARS: Final = frozenset("*?[")


def is_secret_name(name: str) -> bool:
    """True when a variable name looks like it holds a credential."""
    upper = name.upper()
    return upper.endswith("_KEY") or any(word in upper for word in SECRET_WORDS)


def child_env(
    allow: Iterable[str],
    extra: Mapping[str, str] | None = None,
    *,
    base: Mapping[str, str] = os.environ,
) -> dict[str, str]:
    """The environment for a child: `base` filtered by `allow`, then `extra`.

    A pattern containing any of `*?[` is a glob and never keeps a secret-shaped
    name; any other pattern is an exact name and keeps it even when
    secret-shaped (secrets pass only when named exactly). On Windows names
    compare case-insensitively, elsewhere case-sensitively. `extra`, the
    agent's own configured environment, is merged on top unconditionally.
    `allow` empty gives only `extra`. Returns a new dict; `base` is untouched.
    """
    fold = str.casefold if sys.platform == "win32" else str
    exact: set[str] = set()
    globs: list[str] = []
    for pattern in allow:
        if _GLOB_CHARS & set(pattern):
            globs.append(fold(pattern))
        else:
            exact.add(fold(pattern))
    env: dict[str, str] = {}
    for name, value in base.items():
        key = fold(name)
        if key in exact or (
            not is_secret_name(name) and any(fnmatch.fnmatchcase(key, p) for p in globs)
        ):
            env[name] = value
    if extra:
        env.update(extra)
    return env
