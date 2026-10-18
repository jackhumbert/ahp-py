"""Names a deployment may give a custom verb (plan M12).

Standard library only: `options` imports this, and `verbs` imports `journal`, which
imports `options`, so the names live apart to keep the import graph acyclic.
"""

from __future__ import annotations

import builtins
import keyword
import re
from typing import Final

# No leading underscore, so no dunder can shadow `__builtins__` in the Python runner.
VERB_NAME: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

# Claude Code's verbs and runtime names (plan M12).
RESERVED_VERBS: Final = (
    "agent",
    "parallel",
    "pipeline",
    "phase",
    "log",
    "args",
    "budget",
    "workflow",
    "meta",
)

# Replacing these is how a deployment gives approve its own meaning.
REPLACEABLE_VERBS: Final = ("ask", "approve", "canvas")

JS_RESERVED_WORDS: Final = frozenset(
    (
        "await",
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "debugger",
        "default",
        "delete",
        "do",
        "else",
        "enum",
        "export",
        "extends",
        "false",
        "finally",
        "for",
        "function",
        "if",
        "implements",
        "import",
        "in",
        "instanceof",
        "interface",
        "let",
        "new",
        "null",
        "package",
        "private",
        "protected",
        "public",
        "return",
        "static",
        "super",
        "switch",
        "this",
        "throw",
        "true",
        "try",
        "typeof",
        "var",
        "void",
        "while",
        "with",
        "yield",
        "undefined",
        "NaN",
        "Infinity",
        "eval",
        "arguments",
        "globalThis",
    )
)


def _names(text: str) -> frozenset[str]:
    return frozenset(text.split())


# Standard globals of the JavaScript sandbox; a verb of the same name would replace one.
JS_GLOBALS: Final = _names(
    "globalThis Object Function Array String Number Boolean Symbol BigInt Math JSON Date "
    "Intl Promise Reflect Proxy Error AggregateError EvalError RangeError ReferenceError "
    "SyntaxError TypeError URIError Map Set WeakMap WeakSet WeakRef FinalizationRegistry "
    "RegExp ArrayBuffer SharedArrayBuffer DataView Int8Array Uint8Array Uint8ClampedArray "
    "Int16Array Uint16Array Int32Array Uint32Array Float32Array Float64Array "
    "BigInt64Array BigUint64Array Atomics WebAssembly Iterator eval isNaN isFinite "
    "parseInt parseFloat encodeURI encodeURIComponent decodeURI decodeURIComponent "
    "escape unescape undefined NaN Infinity console"
)

# Names the sandbox keeps absent from a script (plan section 3); a verb must not bring one back.
ABSENT_GLOBALS: Final = _names(
    "require process import module exports Buffer setTimeout setInterval setImmediate "
    "clearTimeout clearInterval queueMicrotask structuredClone fetch"
)

# What the Python runner's scripts find in `builtins` (and a verb must not replace).
PY_BUILTINS: Final = frozenset(n for n in dir(builtins) if VERB_NAME.fullmatch(n))


def verb_name_error(name: object) -> str | None:
    """Why *name* cannot name a custom verb, or None."""
    if not isinstance(name, str) or not VERB_NAME.fullmatch(name):
        return f"{name!r} is not a verb name: it must match [A-Za-z][A-Za-z0-9_]*"
    if name in RESERVED_VERBS:
        return f"{name!r} is reserved: Claude Code's runtime defines {', '.join(RESERVED_VERBS)}"
    if keyword.iskeyword(name) or name in JS_RESERVED_WORDS:
        return f"{name!r} is a reserved word in JavaScript or Python"
    if name in JS_GLOBALS or name in ABSENT_GLOBALS:
        return (
            f"{name!r} is refused as a verb name: it would shadow a JavaScript global "
            "the sandbox defines or keeps absent"
        )
    if name in PY_BUILTINS:
        return f"{name!r} is refused as a verb name: it would shadow a Python builtin"
    return None
