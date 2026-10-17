"""Credential masking before anything is published or stored (plan §4.9).

Pattern based and best effort: it catches common credential shapes, not every secret. The inner
agent still sees the real text; only what leaves the run is masked.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

MASK: Final = "[redacted]"

# (name, regex). Only the PEM pattern may span a newline: bearer and key-value use [ \t], never
# \s, between their parts, which is what lets StreamMasker mask line by line. The {0,64} bounds
# keep the key-value scan linear per word start.
BUILTIN_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    ("aws-access-key-id", r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
    ("github-token", r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
    ("github-pat", r"\bgithub_pat_[A-Za-z0-9_]{22,255}\b"),
    ("gitlab-token", r"\bglpat-[A-Za-z0-9_-]{20,}"),
    ("slack-token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    (
        "private-key",
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
        r"(?:[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----|[\s\S]*\Z)",
    ),
    (
        "bearer-token",
        r"(?i)\bbearer[ \t]+(?P<secret>(?=[A-Za-z0-9._~+/=-]*\d)[A-Za-z0-9._~+/=-]{16,})",
    ),
    (
        "key-value-secret",
        r"(?i)\b[\w.-]{0,64}?(?:api[_-]?key|secret|token|passw(?:or)?d|access[_-]?key)"
        r"[\w.-]{0,64}[\"']?[ \t]*[:=][ \t]*[\"']?(?P<secret>[^\s\"',;]{8,})",
    ),
)

# Mapping keys whose value is a secret whatever it looks like: the name list of the key-value
# rule above, plus private keys, auth and credentials. `auth` must be a whole word (not `author`).
SECRET_KEY_NAME: Final = re.compile(
    r"api[_-]?key|secret|token|passw(?:or)?d|access[_-]?key|private[_-]?key|credential"
    r"|(?:^|[^a-z])auth(?:orization)?(?:$|[^a-z])",
    re.IGNORECASE,
)

_BEGIN: Final = "-----BEGIN "
# A block is closed only by a complete END marker followed by a newline.
_END: Final = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")


def _replace(match: re.Match[str]) -> str:
    if "secret" in match.re.groupindex and match.group("secret") is not None:
        start, end = match.span("secret")
        offset = match.start()
        whole = match.group(0)
        return whole[: start - offset] + MASK + whole[end - offset :]
    return MASK


class Redactor:
    def __init__(self, patterns: Sequence[str] = (), *, builtin: bool = True) -> None:
        compiled: list[re.Pattern[str]] = []
        if builtin:
            compiled.extend(re.compile(regex) for _, regex in BUILTIN_PATTERNS)
        for p in patterns:
            rx = re.compile(p)
            if rx.search("") is not None:
                raise ValueError(f"pattern {p!r} matches the empty string")
            compiled.append(rx)
        self._patterns = tuple(compiled)
        self._keys = builtin

    @property
    def enabled(self) -> bool:
        return bool(self._patterns)

    def mask(self, text: str) -> str:
        for rx in self._patterns:
            text = rx.sub(_replace, text)
        return text

    def mask_value(self, value: Any, *, secret: bool = False) -> Any:
        """*value* with credentials masked; *secret* masks every string leaf (a secret key)."""
        if isinstance(value, str):
            return MASK if secret and value else self.mask(value)
        if isinstance(value, Mapping):
            return {
                k: self.mask_value(
                    v, secret=secret or (self._keys and bool(SECRET_KEY_NAME.search(str(k))))
                )
                for k, v in value.items()
            }
        if isinstance(value, list | tuple):
            return [self.mask_value(v, secret=secret) for v in value]
        return value

    def stream(self) -> StreamMasker:
        return StreamMasker(self)


class StreamMasker:
    """Masks streamed deltas a line at a time, holding back an open PEM block."""

    def __init__(self, redactor: Redactor) -> None:
        self._redactor = redactor
        self._buf = ""

    def feed(self, text: str) -> str:
        self._buf += text
        buf = self._buf
        # An END only closes a block once its line is complete: otherwise the cut
        # lands on a newline inside the block and releases it unterminated.
        nl = buf.rfind("\n")
        ends = [m.end() for m in _END.finditer(buf) if m.end() <= nl]
        open_at = buf.find(_BEGIN, ends[-1] if ends else 0)
        limit = open_at if open_at >= 0 else len(buf)
        cut = buf.rfind("\n", 0, limit) + 1
        self._buf = buf[cut:]
        return self._redactor.mask(buf[:cut])

    def flush(self) -> str:
        rest, self._buf = self._buf, ""
        return self._redactor.mask(rest)


NO_REDACTION: Final = Redactor(builtin=False)
