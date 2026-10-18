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
  `run` gets RUN_ACTIVE. `run` also takes `child?: bool` (the run is a child:
  its `workflow()` always throws NEST_MESSAGE) and `spent?: int` (the initial
  budget mirror).

A Python runner (`pyrunner`, standard library only) speaks this same protocol.

Node -> Python methods, only while a `run` is active:

- request `agent` {prompt: str, opts: object, phase: str | null,
  label: str | null} -> result {value: any, spent: int}, where `spent` is the
  run's total output tokens after this call. `phase` is the call's own override
  (`opts.phase` when a string) or null. The host takes the ordinal (0-based, from
  `Scheduler.issue`) and fills in the run's current phase when the line arrives, so
  within one runner they follow issue order. A refused call gets error
  AGENT_LIMIT or BUDGET_EXHAUSTED with `data: {spent: int}`, which `agent()`
  rethrows in the script with the error message; it takes no ordinal.
- request `workflow` {ref: str | {scriptPath: str}, args?: any} -> result
  {source: str, filename: str, spent?: int} (run the child here, in the same
  language) or {value: any, spent?: int} (the host ran it). `args` is present
  only when the call passed them (JSON). The runner mirrors `spent`, which only
  ever increases. A reference that cannot be resolved gets error WORKFLOW_ERROR,
  whose message `workflow()` throws in the script.
  A child of the other language (a `.py` child of a JS parent, or the reverse)
  is run by the host, in a runner of its own started with `child: true` and the
  run's current `spent`; it shares the host's Scheduler, so the agent counter,
  phase, budget, caps and abort are the run's, and only `args` and the returned
  value cross as JSON. The parent gets `{value, spent}`. That runner's own
  `workflow()` is answered with WORKFLOW_ERROR NEST_MESSAGE.
- request `gate` {kind: 'ask' | 'approve', question: str, options: object} ->
  result {value: any}: `ask` gives the answer or null, `approve` true or false.
  `options` holds only what the script set (`choices`/`schema` for ask, `diff`
  for approve). The host takes the gate ordinal, from a counter of its own
  (separate from the agent ordinals), when the line arrives. Error GATE_ERROR
  (no client can answer here), whose message the verb throws, or INVALID_PARAMS
  for a malformed request.
- notification `canvas` {url: str, title: str | null}
- notification `log` {message: str}
- notification `phase` {title: str}; sets the run's current phase at the host.

`parallel` and `pipeline` are context-only globals: plain promises in the
script's realm that never cross the wire; only the `agent()` calls inside them do.

The numeric error codes below are repeated at the top of `runner.mjs`.
"""

from __future__ import annotations

from typing import Final

JSONRPC: Final = "2.0"

# Repeated from engine.options / runner.mjs, kept equal by tests.
PYRUNNER_MODULE: Final = "ahp_host_flow.sandbox.pyrunner"
DEFAULT_MAX_AGENTS: Final = 1000
DEFAULT_MAX_ITEMS: Final = 4096
NEST_MESSAGE: Final = "workflow() can only nest one level: a child workflow cannot call workflow()"

SCRIPT_ERROR: Final = -32000  # the script threw
META_ERROR: Final = -32001  # meta is not a pure literal
SYNTAX_ERROR: Final = -32002  # the source did not parse, or used import/export
RUN_ACTIVE: Final = -32003
AGENT_LIMIT: Final = -32004  # agent() refused: per-run call cap
BUDGET_EXHAUSTED: Final = -32005  # agent() refused: output-token budget spent
WORKFLOW_ERROR: Final = -32006  # workflow() reference could not be resolved
GATE_ERROR: Final = -32007  # ask()/approve() has no answer path

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
