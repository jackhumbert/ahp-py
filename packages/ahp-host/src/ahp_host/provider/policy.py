"""`ToolPolicy`: the pure command and path policy (plan M14 item 3).

A provider describes each tool call it is about to run as a `ToolRequest`
(`kind` is one of `KINDS`) and asks `ToolPolicy.decide` for a `Decision`.
The policy never runs anything and holds no state; the only side effect is
filesystem resolution of paths (symlinks, `..`).

A deny is final. `decide` combines every verdict (the command rules, the path
rules, the `check` callable) and the most restrictive wins: deny over ask over
allow, the first reason of the winning rank is kept. A provider must never
downgrade a deny, so a deny stays a deny in every approval mode (auto-approve
and bypass included); an ask may be answered by the user, a deny may not.

Where the shell text is ambiguous the matcher takes every reading and denies
if any matches: an unknown option may or may not take an argument, so both are
tried; every wrapper (`env`, `sudo`, `timeout`, `xargs`, `nice`, ...) is
matched both as written and unwrapped; `eval`, `sh -c` and `$(...)` bodies are
parsed again as commands. A command's words (and its redirection targets) are
also checked against `deny_paths` and `ask_paths` as paths.

Known limits: this is a best effort over shell text, not a sandbox. Paths built
at run time (variables, `$(...)` output, `cd` earlier in the same command),
globs the shell expands (`cat .en*`) and anything a program reads on its own are
not seen; a command with substitution asks instead.

Standard library only (`provider/` may import nothing else but the protocol
package).
"""

from __future__ import annotations

import fnmatch
import functools
import importlib
import os
import re
import shlex
import sys
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

KINDS: Final = ("command", "read", "write", "tool")

_PUNCT: Final = ";&|()<>\n"
_ASSIGN: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELLS: Final = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "fish"})
_POWERSHELLS: Final = frozenset({"powershell", "pwsh"})
#: Words that start a segment without being its program.
_KEYWORDS: Final = frozenset(
    {"if", "then", "do", "else", "elif", "while", "until", "{", "}", "!", "(", ")"}
)
_MAX_DEPTH: Final = 3
#: Readings of one segment before the command counts as unparseable (denied).
_MAX_READINGS: Final = 64
_RANK: Final = {"allow": 0, "ask": 1, "deny": 2}
_SUBSTITUTIONS: Final = ("$(", "<(", ">(", "`")


@dataclass(frozen=True)
class _Wrapper:
    """A program that runs the rest of its words as a command."""

    #: Options that take the next word (or an attached value: `-n10`).
    takes: frozenset[str] = frozenset()
    #: Options that take none.
    plain: frozenset[str] = frozenset()
    #: Options whose argument is more words (`env -S "git push"`).
    split: frozenset[str] = frozenset()
    #: `NAME=value` words may follow the options.
    assignments: bool = False
    #: Words between the options and the command (`timeout`'s duration).
    operands: int = 0


def _w(
    takes: str = "",
    plain: str = "",
    split: str = "",
    *,
    assignments: bool = False,
    operands: int = 0,
) -> _Wrapper:
    return _Wrapper(
        frozenset(takes.split()),
        frozenset(plain.split()),
        frozenset(split.split()),
        assignments,
        operands,
    )


#: Any option not listed is tried both ways (with and without an argument).
_WRAPPERS: Final[Mapping[str, _Wrapper]] = {
    "env": _w(
        "-u --unset -C --chdir",
        "- -i -0 -v --ignore-environment --null --debug",
        "-S --split-string",
        assignments=True,
    ),
    "sudo": _w(
        "-u -g -h -p -C -D -r -t -U -T --user --group --host --prompt --close-from"
        " --chdir --role --type --other-user --command-timeout",
        "-A -B -b -E -e -H -i -K -k -l -N -n -P -S -s -V -v --login --shell"
        " --preserve-env --non-interactive --stdin --background",
        assignments=True,
    ),
    "doas": _w("-u -C", "-n -s -L"),
    "command": _w("", "-p -v -V"),
    "builtin": _w(),
    "exec": _w("-a", "-c -l"),
    "nohup": _w(),
    "time": _w("-o -f --output --format", "-p -a -v -q --append --verbose --quiet --portability"),
    "nice": _w("-n --adjustment"),
    "timeout": _w(
        "-s -k --signal --kill-after", "-v -f --verbose --foreground --preserve-status", operands=1
    ),
    "xargs": _w(
        "-I -n -P -L -s -d -E -a --max-args --max-procs --max-lines --max-chars --delimiter"
        " --eof --arg-file --process-slot-var",
        "-0 -r -t -p -x -o -i -l -e --null --no-run-if-empty --verbose --interactive --exit"
        " --open-tty --replace",
    ),
    "stdbuf": _w("-i -o -e --input --output --error"),
    "setsid": _w("", "-c -f -w --ctty --fork --wait"),
}
WRAPPERS: Final = frozenset(_WRAPPERS)

_Readings = list[list[str]]
#: One segment of a command: every reading of its words, and the operator after it.
_Segment = tuple[_Readings, str]


@dataclass(frozen=True)
class ToolRequest:
    """One tool call to judge.

    `input` is `{"command": str}` for a command, `{"paths": tuple[str, ...]}`
    for a read or write, `{}` for any other tool. `raw` is the tool's own
    input, for `ToolPolicy.check`.
    """

    name: str
    kind: str
    input: Mapping[str, Any]
    cwd: Path
    roots: tuple[Path, ...]
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    action: Literal["allow", "deny", "ask"]
    reason: str | None = None


ALLOW: Final = Decision("allow")


class PolicyError(ValueError):
    """A `[policy]` table is malformed."""


class _UnparseableError(Exception):
    pass


def _program(word: str) -> str:
    base = re.split(r"[/\\]", word)[-1].casefold()
    for suffix in (".exe", ".cmd", ".bat"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _tokenize(text: str, targets: list[str] | None = None) -> list[tuple[list[str], str]]:
    """Split shell text into (words, operator after) segments. Raises ValueError.

    Redirection targets are left out of the words and appended to `targets`.
    `(` and `)` end a segment, so a subshell or `$(...)` body is a segment of
    its own.
    """
    lexer = shlex.shlex(text, posix=True, punctuation_chars=_PUNCT)
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    segments: list[tuple[list[str], str]] = []
    words: list[str] = []
    skip = False

    def close(op: str) -> None:
        nonlocal words
        if words:
            segments.append((words, op))
        elif segments and op in ("|", "||", "&&"):
            # `( a ) | b`: the `)` closed the segment, the pipe is its operator.
            segments[-1] = (segments[-1][0], op)
        words = []

    for token in lexer:
        if not all(c in _PUNCT for c in token):
            if skip:
                skip = False
                if targets is not None:
                    targets.append(token)
            else:
                words.append(token)
            continue
        skip = False
        if "<" in token or ">" in token:
            skip = True
        elif "|" in token:
            close("||" if "||" in token else "|")
        elif any(c in token for c in ";&\n()"):
            close("&&" if "&&" in token else ";")
    close("")
    return segments


def _substitutions(text: str) -> Iterator[str]:
    """The bodies of `$(...)`, `<(...)`, `>(...)` and backticks, outermost first.

    Quotes are ignored on purpose: a body inside single quotes is not run by
    the shell, but checking it anyway can only deny more. An unclosed one runs
    to the end of the text.
    """
    i = 0
    while i < len(text):
        if text[i] == "`":
            end = text.find("`", i + 1)
            end = len(text) if end < 0 else end
            yield text[i + 1 : end]
            i = end + 1
        elif text.startswith(("$(", "<(", ">("), i):
            depth, j = 1, i + 2
            while j < len(text) and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            yield text[i + 2 : j - 1 if depth == 0 else j]
            i = j
        else:
            i += 1


def _after_options(spec: _Wrapper, rest: list[str]) -> _Readings:
    """What a wrapper runs, in every reading of its options."""
    found: _Readings = []

    def finish(i: int) -> None:
        if spec.assignments:
            while i < len(rest) and _ASSIGN.match(rest[i]):
                i += 1
        found.append(rest[i + spec.operands :])

    def walk(i: int) -> None:
        if len(found) > _MAX_READINGS:
            raise _UnparseableError
        word = rest[i] if i < len(rest) else "--"
        if i >= len(rest):
            finish(i)
        elif word == "--":
            finish(i + 1)
        elif word in spec.plain:
            walk(i + 1)
        elif not word.startswith("-") or len(word) == 1:
            finish(i)
        else:
            option(i, word)

    def option(i: int, word: str) -> None:
        name, eq, value = word.partition("=")
        attached = not word.startswith("--") and len(word) > 2
        if name in spec.split or (attached and word[:2] in spec.split):
            if eq:
                words, after = value, i + 1
            elif attached:
                words, after = word[2:], i + 1
            else:
                words, after = (rest[i + 1] if i + 1 < len(rest) else ""), i + 2
            found.extend(_after_options(spec, shlex.split(words) + rest[after:]))
        elif eq or name in spec.plain:
            walk(i + 1)
        elif name in spec.takes:
            walk(i + 2)
        elif attached and word[:2] in spec.takes:
            walk(i + 1)
        else:
            # Unknown: it may or may not take the next word.
            walk(i + 1)
            walk(i + 2)

    walk(0)
    return found


def _unwrap(words: list[str]) -> _Readings:
    """The next readings of a segment with one layer stripped; [] when none."""
    if _ASSIGN.match(words[0]):
        i = 0
        while i < len(words) and _ASSIGN.match(words[i]):
            i += 1
        return [words[i:]]
    if words[0] in _KEYWORDS:
        return [words[1:]]
    spec = _WRAPPERS.get(_program(words[0]))
    return _after_options(spec, words[1:]) if spec is not None else []


def _readings(words: list[str]) -> _Readings:
    """Every reading of a segment: as written, and with each wrapper stripped."""
    found: _Readings = []
    seen: set[tuple[str, ...]] = set()
    pending = [words]
    while pending:
        current = pending.pop()
        key = tuple(current)
        if not current or key in seen:
            continue
        seen.add(key)
        if len(seen) > _MAX_READINGS:
            raise _UnparseableError
        found.append(current)
        pending.extend(_unwrap(current))
    return found


def _scripts(words: list[str]) -> Iterator[str]:
    """Scripts a reading carries (`bash -c SCRIPT`, `cmd /c ...`, `eval ...`)."""
    prog = _program(words[0])
    rest = words[1:]
    if prog == "eval":
        yield " ".join(rest)
    elif prog in _SHELLS:
        for i, word in enumerate(rest):
            if word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
                if i + 1 < len(rest):
                    yield rest[i + 1]
                return
    elif prog == "cmd" or prog in _POWERSHELLS:
        flags = {"/c", "/k"} if prog == "cmd" else {"-c", "-command"}
        for i, word in enumerate(rest):
            if word.casefold() in flags:
                yield " ".join(rest[i + 1 :])
                return


def _sequences(text: str, targets: list[str], depth: int = 0) -> list[list[_Segment]]:
    """The command's segments, plus those of every script and substitution in it."""
    if depth > _MAX_DEPTH:
        raise _UnparseableError
    scripts = list(_substitutions(text))
    segments: list[_Segment] = []
    for words, op in _tokenize(text, targets):
        readings = _readings(words)
        if readings:
            segments.append((readings, op))
        for reading in readings:
            scripts.extend(_scripts(reading))
    found = [segments]
    for script in scripts:
        found.extend(_sequences(script, targets, depth + 1))
    return found


@functools.lru_cache(maxsize=256)
def _parse_pattern(pattern: str) -> tuple[tuple[tuple[str, ...], str], ...]:
    try:
        segments = _tokenize(pattern)
    except ValueError:
        segments = [(pattern.split(), "")]
    return tuple((tuple(words), op) for words, op in segments if words)


def _reachable(words: list[str], start: int) -> list[int]:
    """Where the next pattern word may be, skipping options from `start`.

    An option may or may not take the next word as its argument (`git -C x
    push`, `git --no-pager push`), so both are followed; `--opt=value` is one
    word.
    """
    seen: set[int] = set()
    stack = [start]
    while stack:
        k = stack.pop()
        if k >= len(words) or k in seen:
            continue
        seen.add(k)
        word = words[k]
        if word.startswith("-") and len(word) > 1:
            stack.append(k + 1)
            if "=" not in word:
                stack.append(k + 2)
    return sorted(seen)


def _match_words(pat: tuple[str, ...], words: list[str]) -> bool:
    memo: dict[tuple[int, int], bool] = {}

    def match(i: int, j: int) -> bool:
        key = (i, j)
        if key not in memo:
            memo[key] = False
            memo[key] = step(i, j)
        return memo[key]

    def step(i: int, j: int) -> bool:
        if i == len(pat):
            return True
        if pat[i] == "*":
            return any(match(i + 1, k) for k in range(j, len(words) + 1))
        if i == 0:
            if not words or not fnmatch.fnmatchcase(_program(words[0]), pat[0].casefold()):
                return False
            return match(1, 1)
        return any(
            fnmatch.fnmatchcase(words[k], pat[i]) and match(i + 1, k + 1)
            for k in _reachable(words, j)
        )

    return match(0, 0)


def _match_sequence(pats: tuple[tuple[tuple[str, ...], str], ...], seq: list[_Segment]) -> bool:
    n = len(pats)
    for start in range(len(seq) - n + 1):
        window = seq[start : start + n]
        if all(op == p[1] for p, (_, op) in zip(pats[:-1], window, strict=False)) and all(
            any(_match_words(p[0], reading) for reading in readings)
            for p, (readings, _) in zip(pats, window, strict=True)
        ):
            return True
    return False


def _command_words(sequences: list[list[_Segment]], targets: list[str]) -> list[str]:
    """Words of a command that may name a path: operands, `--opt=value`'s value,
    the value of `NAME=value`, and redirection targets."""
    found: dict[str, None] = dict.fromkeys(targets)
    for segments in sequences:
        for readings, _ in segments:
            for reading in readings:
                for word in reading:
                    if "=" in word:
                        found[word.partition("=")[2]] = None
                    if not word.startswith("-"):
                        found[word] = None
    found.pop("", None)
    return list(found)


def _regex(pattern: str) -> re.Pattern[str]:
    # Case-insensitive on every platform: both sides are casefolded, so a deny
    # written `**/.env` also covers `.ENV` on a case-insensitive filesystem.
    pattern = os.path.expanduser(pattern)
    if sys.platform == "win32":
        pattern = pattern.replace("\\", "/")
    pattern = pattern.casefold()
    out = [] if Path(pattern).is_absolute() or pattern.startswith("/") else ["(?:[^/]*/)*"]
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:[^/]*/)*")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append("(?:/.*)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out), re.IGNORECASE)


def _first_match(patterns: tuple[str, ...], candidates: tuple[Path, ...]) -> str | None:
    for pattern in patterns:
        regex = _regex(pattern)
        if any(regex.fullmatch(c.as_posix().casefold()) for c in candidates):
            return pattern
    return None


def _candidates(raw: str, cwd: Path) -> tuple[Path, Path]:
    """A path as written (normalised) and resolved. Raises OSError, ValueError."""
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = cwd / p
    return Path(os.path.normpath(p)), p.resolve(strict=False)


def _combine(decisions: list[Decision]) -> Decision:
    best = ALLOW
    for decision in decisions:
        if _RANK[decision.action] > _RANK[best.action]:
            best = decision
    return best


@dataclass(frozen=True)
class ToolPolicy:
    """Rules over commands and paths; see the module docstring and the README."""

    deny_commands: tuple[str, ...] = ()
    deny_paths: tuple[str, ...] = ()
    ask_paths: tuple[str, ...] = ()
    allow_paths_outside_roots: bool = False
    check: Callable[[ToolRequest], Decision | None] | None = None

    def decide(self, request: ToolRequest) -> Decision:
        decisions: list[Decision] = []
        if request.kind == "command":
            decisions.extend(self._command(request))
        elif request.kind in ("read", "write"):
            decisions.extend(self._paths(request))
        elif request.kind != "tool":
            decisions.append(Decision("ask", f"policy asks: unknown tool kind {request.kind!r}"))
        if self.check is not None:
            decisions.extend(self._run_check(request))
        return _combine(decisions)

    def _run_check(self, request: ToolRequest) -> list[Decision]:
        assert self.check is not None
        try:
            result = self.check(request)
        except Exception as exc:
            return [Decision("deny", f"denied by policy: the check failed: {exc}")]
        if result is None:
            return []
        if not isinstance(result, Decision):
            return [Decision("deny", "denied by policy: the check returned a non-Decision")]
        if result.action != "allow" and result.reason is None:
            reason = (
                "denied by the policy check" if result.action == "deny" else "the policy check asks"
            )
            return [Decision(result.action, reason)]
        return [result]

    def _command(self, request: ToolRequest) -> list[Decision]:
        path_rules = bool(self.deny_paths or self.ask_paths)
        if not self.deny_commands and not path_rules:
            return []
        command = request.input.get("command")
        if not isinstance(command, str) or not command.strip():
            return [Decision("ask", "policy asks: no command to check")]
        decisions: list[Decision] = []
        if any(s in command for s in _SUBSTITUTIONS):
            decisions.append(Decision("ask", "policy asks: the command uses command substitution"))
        targets: list[str] = []
        try:
            sequences = _sequences(command, targets)
        except (ValueError, _UnparseableError):
            decisions.append(Decision("deny", "denied by policy: the command could not be parsed"))
            return decisions
        for pattern in self.deny_commands:
            pats = _parse_pattern(pattern)
            if pats and any(_match_sequence(pats, seq) for seq in sequences):
                decisions.append(
                    Decision("deny", f"denied by policy: the command matches {pattern!r}")
                )
                break
        if path_rules:
            decisions.extend(self._command_paths(request.cwd, _command_words(sequences, targets)))
        return decisions

    def _command_paths(self, cwd: Path, words: list[str]) -> list[Decision]:
        """A command's words judged as paths: `deny_paths` and `ask_paths` only.

        Not the roots check, since most words are not paths (`ls /usr` is
        fine); a word that cannot be resolved is matched as written.
        """
        decisions: list[Decision] = []
        for word in words:
            try:
                candidates: tuple[Path, ...] = _candidates(word, cwd)
            except (OSError, RuntimeError, ValueError):
                candidates = (Path(word),)
            shown = candidates[-1]
            hit = _first_match(self.deny_paths, candidates)
            if hit is not None:
                decisions.append(
                    Decision("deny", f"denied by policy: the command names {shown} ({hit!r})")
                )
                break
            hit = _first_match(self.ask_paths, candidates)
            if hit is not None:
                decisions.append(
                    Decision("ask", f"policy asks before a command on {shown} ({hit!r})")
                )
        return decisions

    def _paths(self, request: ToolRequest) -> list[Decision]:
        raw_paths = request.input.get("paths") or ()
        if not raw_paths:
            if self.deny_paths or self.ask_paths:
                return [Decision("ask", "policy asks: no path to check")]
            return []
        verb = "reading" if request.kind == "read" else "writing"
        roots = []
        for root in request.roots:
            try:
                roots.append(root.resolve())
            except (OSError, RuntimeError, ValueError):
                continue
        decisions: list[Decision] = []
        for raw in raw_paths:
            try:
                candidates = _candidates(str(raw), request.cwd)
            except (OSError, RuntimeError, ValueError) as exc:
                decisions.append(
                    Decision("deny", f"denied by policy: {raw!r} could not be resolved: {exc}")
                )
                continue
            resolved = candidates[1]
            hit = _first_match(self.deny_paths, candidates)
            if hit is not None:
                decisions.append(Decision("deny", f"denied by policy: {resolved} matches {hit!r}"))
                continue
            if not self.allow_paths_outside_roots and not any(
                resolved.is_relative_to(r) for r in roots
            ):
                decisions.append(
                    Decision(
                        "deny", f"denied by policy: {resolved} is outside the session's folders"
                    )
                )
                continue
            hit = _first_match(self.ask_paths, candidates)
            if hit is not None:
                decisions.append(Decision("ask", f"policy asks before {verb} {resolved} ({hit!r})"))
        return decisions


def load_check(path: str) -> Callable[[ToolRequest], Decision | None]:
    """Import a `pkg.mod:fn` check callable. Raises PolicyError."""
    if not isinstance(path, str) or ":" not in path:
        raise PolicyError("[policy] check must be an import path 'pkg.mod:fn'")
    module, _, attr = path.partition(":")
    try:
        fn = getattr(importlib.import_module(module), attr)
    except Exception as exc:
        raise PolicyError(f"[policy] check {path!r}: {exc}") from exc
    if not callable(fn):
        raise PolicyError(f"[policy] check {path!r} is not callable")
    check: Callable[[ToolRequest], Decision | None] = fn
    return check


def policy_from_table(table: Mapping[str, Any], *, extra: Collection[str] = ()) -> ToolPolicy:
    """Build a policy from a `[policy]` table; keys in `extra` are the caller's."""
    allowed = {"deny_commands", "deny_paths", "ask_paths", "allow_paths_outside_roots", "check"}
    unknown = set(table) - allowed - set(extra)
    if unknown:
        raise PolicyError(f"[policy]: unknown key(s): {', '.join(sorted(unknown))}")
    kwargs: dict[str, Any] = {}
    for key in ("deny_commands", "deny_paths", "ask_paths"):
        if key not in table:
            continue
        value = table[key]
        if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
            raise PolicyError(f"[policy] {key} must be a list of strings")
        kwargs[key] = tuple(value)
    if "allow_paths_outside_roots" in table:
        value = table["allow_paths_outside_roots"]
        if not isinstance(value, bool):
            raise PolicyError("[policy] allow_paths_outside_roots must be true or false")
        kwargs["allow_paths_outside_roots"] = value
    if "check" in table:
        value = table["check"]
        if not isinstance(value, str) or ":" not in value:
            raise PolicyError("[policy] check must be an import path 'pkg.mod:fn'")
        kwargs["check"] = load_check(value)
    return ToolPolicy(**kwargs)
