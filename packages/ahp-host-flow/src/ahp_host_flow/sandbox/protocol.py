"""Wire protocol between Python and the Node sandbox (`runner.mjs`).

JSON-RPC 2.0 over the Node child's stdin and stdout. Each message is one UTF-8
JSON object on one line ending in "\\n". Python writes with `ensure_ascii=True`,
because Node's readline also ends a line at a raw U+2028 or U+2029, which
`json.dumps` would otherwise emit unescaped. Node's output is read by asyncio,
which splits on "\\n" only. An error response with a null id (a frame Node
could not parse) fails every pending request. stderr carries diagnostics only.

A message with `method` is a request (it has `id`) or a notification (no `id`).
A message without `method` is a response. Each side numbers its own requests
with integers from 1 and matches responses only against its own pending map, so
the two id spaces never collide.

Python -> Node methods:

- `meta` {source: str, filename: str} -> result {meta: object | null}
- `run` {source: str, filename: str, args?: any, limits?: {maxAgents: int,
  budgetTotal: int | null, maxItems: int}} -> result {value: any}. If `args` is
  absent the script sees `args === undefined`; if `limits` is absent the runner
  uses 1000 / null / 4096. One run at a time per process: a second concurrent
  `run` gets RUN_ACTIVE.

Node -> Python methods, only while a `run` is active:

- request `agent` {ordinal: int, prompt: str, opts: object, phase: str | null,
  label: str | null} -> result {value: any, spent: int}, where `spent` is the
  run's total output tokens after this call. A refused call gets error
  AGENT_LIMIT or BUDGET_EXHAUSTED with `data: {spent: int}`, which `agent()`
  rethrows in the script with the error message.
- request `workflow` {ref: str | {scriptPath: str}} -> result {source: str,
  filename: str}. A reference that cannot be resolved gets error WORKFLOW_ERROR,
  whose message `workflow()` throws in the script.
- notification `log` {message: str}
- notification `phase` {title: str}

`parallel` and `pipeline` are context-only globals: plain promises in the
script's realm that never cross the wire; only the `agent()` calls inside them do.

The numeric error codes below are repeated at the top of `runner.mjs`.
"""

from __future__ import annotations

from typing import Final

JSONRPC: Final = "2.0"

SCRIPT_ERROR: Final = -32000  # the script threw
META_ERROR: Final = -32001  # meta is not a pure literal
SYNTAX_ERROR: Final = -32002  # the source did not parse, or used import/export
RUN_ACTIVE: Final = -32003
AGENT_LIMIT: Final = -32004  # agent() refused: per-run call cap
BUDGET_EXHAUSTED: Final = -32005  # agent() refused: output-token budget spent
WORKFLOW_ERROR: Final = -32006  # workflow() reference could not be resolved

INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603
PARSE_ERROR: Final = -32700

# asyncio StreamReader limit: the default 64 KiB would break on one long agent
# reply, and 16 MiB bounds a runaway line.
MAX_LINE_BYTES: Final = 16 * 1024 * 1024
STDERR_TAIL_LINES: Final = 50
# Seconds to wait for node to exit after stdin closes, before killing it.
SHUTDOWN_TIMEOUT: Final = 5.0
