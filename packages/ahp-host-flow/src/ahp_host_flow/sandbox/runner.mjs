// Script sandbox: JSON-RPC 2.0 over stdin/stdout, one JSON object per line.
// The wire protocol is documented in protocol.py.
import readline from 'node:readline';
import vm from 'node:vm';
import { parse } from './vendor/acorn.mjs';

// Error codes: keep in sync with protocol.py.
const SCRIPT_ERROR = -32000;
const META_ERROR = -32001;
const SYNTAX_ERROR = -32002;
const RUN_ACTIVE = -32003;
const AGENT_LIMIT = -32004;
const BUDGET_EXHAUSTED = -32005;
const WORKFLOW_ERROR = -32006;
const GATE_ERROR = -32007;
const VERB_ERROR = -32008;
const METHOD_NOT_FOUND = -32601;
const INVALID_PARAMS = -32602;
const INTERNAL_ERROR = -32603;
const PARSE_ERROR = -32700;

class RpcError extends Error {
  constructor(code, message, data) {
    super(message);
    this.code = code;
    this.data = data;
  }
}

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + '\n');
}

function sendError(id, code, message, data) {
  const error = { code, message };
  if (data !== undefined) error.data = data;
  send({ jsonrpc: '2.0', id, error });
}

function literal(node, path) {
  const line = node.loc.start.line;
  const bad = () =>
    new RpcError(META_ERROR, `meta is not a pure literal: ${path} is a ${node.type} (line ${line})`);
  switch (node.type) {
    case 'Literal':
      if (node.regex || node.bigint !== undefined) throw bad();
      return node.value;
    case 'TemplateLiteral':
      if (node.expressions.length > 0) throw bad();
      return node.quasis[0].value.cooked;
    case 'UnaryExpression':
      if (
        (node.operator === '-' || node.operator === '+') &&
        node.argument.type === 'Literal' &&
        typeof node.argument.value === 'number'
      ) {
        return node.operator === '-' ? -node.argument.value : node.argument.value;
      }
      throw bad();
    case 'ArrayExpression':
      return node.elements.map((el, i) => {
        if (el === null || el.type === 'SpreadElement') throw bad();
        return literal(el, `${path}[${i}]`);
      });
    case 'ObjectExpression': {
      const out = {};
      for (const prop of node.properties) {
        if (
          prop.type !== 'Property' ||
          prop.kind !== 'init' ||
          prop.computed ||
          prop.method ||
          prop.shorthand
        ) {
          throw bad();
        }
        let key;
        if (prop.key.type === 'Identifier') {
          key = prop.key.name;
        } else if (
          prop.key.type === 'Literal' &&
          (typeof prop.key.value === 'string' || typeof prop.key.value === 'number')
        ) {
          key = String(prop.key.value);
        } else {
          throw bad();
        }
        const value = literal(prop.value, `${path}.${key}`);
        Object.defineProperty(out, key, {
          value,
          enumerable: true,
          writable: true,
          configurable: true,
        });
      }
      return out;
    }
    default:
      throw bad();
  }
}

function isMetaExport(node) {
  const d = node.type === 'ExportNamedDeclaration' ? node.declaration : null;
  return (
    d !== null &&
    d !== undefined &&
    d.type === 'VariableDeclaration' &&
    d.kind === 'const' &&
    d.declarations.length === 1 &&
    d.declarations[0].id.type === 'Identifier' &&
    d.declarations[0].id.name === 'meta' &&
    d.declarations[0].init !== null
  );
}

function analyse(source, filename) {
  let ast;
  try {
    ast = parse(source, {
      ecmaVersion: 'latest',
      sourceType: 'module',
      allowReturnOutsideFunction: true,
      allowAwaitOutsideFunction: true,
      locations: true,
    });
  } catch (err) {
    // acorn rejects a second `export const meta` as a redeclaration before we
    // can scan the body; report it as the module-syntax error it is.
    const dup = /^Identifier 'meta' has already been declared/.test(err.message);
    const exports = source.match(/^[ \t]*export\s+const\s+meta\b/gm) ?? [];
    if (dup && exports.length >= 2 && err.loc) {
      throw new RpcError(
        SYNTAX_ERROR,
        `import/export is not allowed in a workflow script (line ${err.loc.line})`,
        { filename },
      );
    }
    throw new RpcError(SYNTAX_ERROR, err.message, { filename });
  }
  let exportNode = null;
  for (const node of ast.body) {
    if (
      node.type === 'ImportDeclaration' ||
      node.type === 'ExportDefaultDeclaration' ||
      node.type === 'ExportAllDeclaration' ||
      node.type === 'ExportNamedDeclaration'
    ) {
      if (exportNode === null && isMetaExport(node)) {
        exportNode = node;
        continue;
      }
      throw new RpcError(
        SYNTAX_ERROR,
        `import/export is not allowed in a workflow script (line ${node.loc.start.line})`,
        { filename },
      );
    }
  }
  if (exportNode === null) return { meta: null, body: source };
  const init = exportNode.declaration.declarations[0].init;
  if (init.type !== 'ObjectExpression') {
    throw new RpcError(META_ERROR, `meta must be an object literal (line ${init.loc.start.line})`);
  }
  const meta = literal(init, 'meta');
  // Blank out `export ` so positions are kept and `const meta` stays.
  const blank = ' '.repeat(exportNode.declaration.start - exportNode.start);
  const body =
    source.slice(0, exportNode.start) + blank + source.slice(exportNode.declaration.start);
  return { meta, body };
}

// Evaluated inside each fresh context. It returns `install`, a context-realm
// function; the host-realm hostCall/hostNotify live only in its closure, so
// the script never holds a host-realm object. Values cross as JSON strings.
// Everything sits in one block: a script's top-level `const` would otherwise
// share the context's global lexical scope with the workflow script, which
// could then call RealDate.now(). The block's completion value is `install`.
const PRELUDE = `'use strict';
{
// V8 installs its own \`console\` in every new context; the script must not see it.
delete globalThis.console;
// Non-deterministic APIs throw; dates built from explicit values still work.
const banned = (api) => {
  throw new Error(api + ' is not allowed in a workflow script: it is not deterministic');
};
const RealDate = Date;
function SafeDate(...a) {
  if (new.target === undefined) banned('Date()');
  if (a.length === 0) banned('new Date()');
  return Reflect.construct(RealDate, a, new.target);
}
SafeDate.prototype = RealDate.prototype;
Object.defineProperty(RealDate.prototype, 'constructor', {
  value: SafeDate,
  writable: true,
  configurable: true,
  enumerable: false,
});
Object.defineProperty(SafeDate, 'name', { value: 'Date' });
SafeDate.UTC = RealDate.UTC;
SafeDate.parse = RealDate.parse;
SafeDate.now = function now() {
  banned('Date.now()');
};
Object.defineProperty(globalThis, 'Date', {
  value: SafeDate,
  writable: true,
  configurable: true,
  enumerable: false,
});
Object.defineProperty(Math, 'random', {
  value: function random() {
    banned('Math.random()');
  },
  writable: true,
  configurable: true,
  enumerable: false,
});
const dtfProto = Intl.DateTimeFormat.prototype;
const formatGetter = Object.getOwnPropertyDescriptor(dtfProto, 'format').get;
Object.defineProperty(dtfProto, 'format', {
  get() {
    const bound = formatGetter.call(this);
    return function format(date) {
      if (date === undefined) banned('Intl.DateTimeFormat format() without a date');
      return bound(date);
    };
  },
  configurable: true,
  enumerable: false,
});
const realFormatToParts = dtfProto.formatToParts;
Object.defineProperty(dtfProto, 'formatToParts', {
  value: function formatToParts(date) {
    if (date === undefined) banned('Intl.DateTimeFormat formatToParts() without a date');
    return realFormatToParts.call(this, date);
  },
  writable: true,
  configurable: true,
  enumerable: false,
});
(function install(hostCall, hostNotify, hostStart, hostState, settingsJson) {
  const settings = JSON.parse(settingsJson);
  const limits = settings.limits;
  const call = async (method, params) => JSON.parse(await hostCall(method, JSON.stringify(params)));
  const define = (name, value) =>
    Object.defineProperty(globalThis, name, {
      value,
      writable: true,
      configurable: true,
      enumerable: false,
    });
  // The spent-token mirror lives in the host (hostState) and crosses as a
  // primitive, so a child workflow's context shares it without holding any of
  // the parent context's objects. The ordinal counter and the current phase live
  // in the Python Scheduler.
  const spentTokens = () => hostState('spent', null);
  const budgetError = () =>
    new Error('budget exhausted: ' + spentTokens() + ' of ' + limits.budgetTotal + ' output tokens spent');
  define('agent', async function agent(prompt, opts) {
    if (typeof prompt !== 'string') throw new TypeError('agent() needs a prompt string');
    if (opts === undefined || opts === null) opts = {};
    if (typeof opts !== 'object' || Array.isArray(opts)) {
      throw new TypeError('agent() opts must be an object');
    }
    opts = JSON.parse(JSON.stringify(opts));
    // First line of defence, from the mirror; Python re-checks as the authority.
    // A refused call consumes no ordinal; the host takes it and fills in the current phase.
    if (limits.budgetTotal !== null && spentTokens() >= limits.budgetTotal) throw budgetError();
    const phase = typeof opts.phase === 'string' ? opts.phase : null;
    const label = typeof opts.label === 'string' ? opts.label : null;
    const reply = await call('agent', { prompt, opts, phase, label });
    const s = reply.ok ? reply.result?.spent : reply.data?.spent;
    if (typeof s === 'number') hostState('spend', s);
    if (!reply.ok) throw new Error(reply.message);
    return reply.result?.value ?? null;
  });
  const tooMany = (fn, n) =>
    new RangeError(fn + '() takes at most ' + limits.maxItems + ' items, got ' + n);
  define('parallel', async function parallel(thunks) {
    if (!Array.isArray(thunks)) throw new TypeError('parallel() needs an array of functions');
    if (thunks.length > limits.maxItems) throw tooMany('parallel', thunks.length);
    return Promise.all(
      thunks.map(async (thunk) => {
        try {
          return await thunk();
        } catch {
          return null;
        }
      }),
    );
  });
  define('pipeline', async function pipeline(items, ...stages) {
    if (!Array.isArray(items)) throw new TypeError('pipeline() needs an array of items');
    if (items.length > limits.maxItems) throw tooMany('pipeline', items.length);
    for (const stage of stages) {
      if (typeof stage !== 'function') throw new TypeError('pipeline() stages must be functions');
    }
    return Promise.all(
      items.map(async (item, index) => {
        let prev = item;
        for (const stage of stages) {
          try {
            prev = await stage(prev, item, index);
          } catch {
            return null;
          }
        }
        return prev;
      }),
    );
  });
  define('workflow', async function workflow(ref, childArgs) {
    if (settings.child) {
      throw new Error('workflow() can only nest one level: a child workflow cannot call workflow()');
    }
    let wire;
    if (typeof ref === 'string') wire = ref;
    else if (ref !== null && typeof ref === 'object' && typeof ref.scriptPath === 'string') wire = { scriptPath: ref.scriptPath };
    else throw new TypeError('workflow() needs a name or {scriptPath}');
    const req = { ref: wire };
    const argsText = childArgs === undefined ? undefined : JSON.stringify(childArgs);
    if (argsText !== undefined) req.args = JSON.parse(argsText);
    const reply = await call('workflow', req);
    if (!reply.ok) throw new Error(reply.message);
    if (typeof reply.result.spent === 'number') hostState('spend', reply.result.spent);
    // The host ran a child of the other language; only its JSON value comes back.
    if (Object.hasOwn(reply.result, 'value')) return reply.result.value ?? null;
    const argsJson = childArgs === undefined ? null : JSON.stringify(childArgs);
    // A child-realm promise of the child's result, or a string saying why it did not start.
    const started = hostStart(reply.result.source, reply.result.filename, argsJson);
    if (typeof started === 'string') throw new Error(started);
    const value = await started;
    const json = JSON.stringify(value);
    return json === undefined ? null : JSON.parse(json);
  });
  const isPlain = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
  const gateOpts = (fn, opts) => {
    if (opts === undefined || opts === null) return {};
    if (!isPlain(opts)) throw new TypeError(fn + '() opts must be an object');
    return opts;
  };
  define('ask', async function ask(question, opts) {
    if (typeof question !== 'string') throw new TypeError('ask() needs a question string');
    opts = gateOpts('ask', opts);
    const { choices, schema } = opts;
    const hasChoices = choices !== undefined && choices !== null;
    const hasSchema = schema !== undefined && schema !== null;
    if (
      hasChoices &&
      !(Array.isArray(choices) && choices.length > 0 && choices.every((c) => typeof c === 'string'))
    ) {
      throw new TypeError('ask() choices must be a non-empty array of strings');
    }
    if (hasSchema && !isPlain(schema)) throw new TypeError('ask() schema must be an object');
    if (hasChoices && hasSchema) throw new TypeError('ask() takes choices or schema, not both');
    const options = {};
    if (hasChoices) options.choices = JSON.parse(JSON.stringify(choices));
    if (hasSchema) options.schema = JSON.parse(JSON.stringify(schema));
    const reply = await call('gate', { kind: 'ask', question, options });
    if (!reply.ok) throw new Error(reply.message);
    return reply.result?.value ?? null;
  });
  define('approve', async function approve(summary, opts) {
    if (typeof summary !== 'string') throw new TypeError('approve() needs a summary string');
    opts = gateOpts('approve', opts);
    const { diff, x } = opts;
    const optional = (v) => v === undefined || v === null || typeof v === 'string';
    if (
      diff !== undefined &&
      diff !== null &&
      typeof diff !== 'string' &&
      !(
        Array.isArray(diff) &&
        diff.every((d) => isPlain(d) && typeof d.path === 'string' && optional(d.before) && optional(d.after))
      )
    ) {
      throw new TypeError('approve() diff must be a string or an array of {path, before?, after?}');
    }
    if (x !== undefined && x !== null) {
      if (!isPlain(x)) throw new TypeError('approve() x must be an object');
      if (x.detail !== undefined && typeof x.detail !== 'boolean') {
        throw new TypeError('approve() x.detail must be a boolean');
      }
    }
    const detail = x != null && x.detail === true;
    const options = {};
    if (diff !== undefined && diff !== null) options.diff = JSON.parse(JSON.stringify(diff));
    if (detail) options.x = { detail: true };
    const reply = await call('gate', { kind: 'approve', question: summary, options });
    if (!reply.ok) throw new Error(reply.message);
    return detail ? reply.result?.value : reply.result?.value === true;
  });
  define('canvas', function canvas(url, opts) {
    if (typeof url !== 'string' || !/^https?:\\/\\/\\S/i.test(url)) {
      throw new TypeError('canvas() needs an http(s) URL');
    }
    opts = gateOpts('canvas', opts);
    const { title } = opts;
    if (title !== undefined && title !== null && typeof title !== 'string') {
      throw new TypeError('canvas() title must be a string');
    }
    hostNotify('canvas', JSON.stringify({ url, title: title ?? null }));
  });
  define('log', function log(message) {
    hostNotify('log', JSON.stringify({ message: String(message) }));
  });
  define('phase', function phase(title) {
    hostNotify('phase', JSON.stringify({ title: String(title) }));
  });
  define(
    'budget',
    Object.freeze({
      total: limits.budgetTotal,
      spent: function spent() {
        return spentTokens();
      },
      remaining: function remaining() {
        return limits.budgetTotal === null ? Infinity : Math.max(0, limits.budgetTotal - spentTokens());
      },
    }),
  );
  define('args', settings.hasArgs ? settings.args : undefined);
  // Custom verbs come last, so a verb may replace ask, approve or canvas. Only JSON
  // crosses: call() clones the arguments and the reply value is parsed in this realm.
  for (const name of Array.isArray(settings.verbs) ? settings.verbs : []) {
    define(
      name,
      {
        [name]: async function (...verbArgs) {
          const reply = await call('verb', { name, args: verbArgs });
          if (!reply.ok) throw new Error(reply.message);
          return reply.result?.value ?? null;
        },
      }[name],
    );
  }
});
}`;

// Node -> Python requests awaiting a response, by id. Responses that arrive
// after a run ends find nothing here and are ignored.
const pending = new Map();
let nextId = 1;
let runActive = false;

// Parses JSON the script handed a hook. The script controls its realm's
// JSON.stringify, so the text can be anything; a host-realm SyntaxError thrown
// back would hand the script the host Function. Throw a primitive instead.
function fromScript(text) {
  if (typeof text !== 'string') throw 'sandbox: a hook was given a non-string';
  try {
    return JSON.parse(text);
  } catch {
    throw 'sandbox: a hook was given invalid JSON';
  }
}

function hostCall(method, paramsJson) {
  const params = fromScript(paramsJson);
  return new Promise((resolve) => {
    const id = nextId++;
    pending.set(id, (msg) => {
      resolve(
        JSON.stringify(
          msg.error
            ? { ok: false, message: String(msg.error.message), data: msg.error.data ?? null }
            : { ok: true, result: msg.result ?? null },
        ),
      );
    });
    send({ jsonrpc: '2.0', id, method, params });
  });
}

function hostNotify(method, paramsJson) {
  send({ jsonrpc: '2.0', method, params: fromScript(paramsJson) });
}

// The stack's header (the error's own lines) and only the frames in the script's file:
// the runner's, Node's and the prelude's frames name host paths and internals.
function scriptFrames(stack, filename) {
  const out = [];
  let inFrames = false;
  for (const line of stack.split('\n')) {
    const t = line.trim();
    if (t.startsWith('at ')) inFrames = true;
    if (!inFrames) {
      out.push(line);
      continue;
    }
    const where = t.replace(/^at (async )?/, '');
    if (where.startsWith(filename + ':') || where.includes('(' + filename + ':')) out.push(line);
  }
  return out.join('\n');
}

function describeThrown(e, filename) {
  // The thrown value is a context-realm object, so `instanceof Object` would be false.
  let message;
  let name = null;
  let stack = null;
  try {
    const objectLike = (typeof e === 'object' && e !== null) || typeof e === 'function';
    message = objectLike && 'message' in e ? String(e.message) : String(e);
  } catch {
    message = 'unprintable thrown value';
  }
  try {
    name = e?.name ?? null;
    if (typeof name !== 'string') name = null;
  } catch {
    name = null;
  }
  try {
    stack = e?.stack ?? null;
    stack = typeof stack === 'string' ? scriptFrames(stack, filename) : null;
  } catch {
    stack = null;
  }
  return { message, data: { name, stack } };
}

async function run(params) {
  if (typeof params.source !== 'string') {
    throw new RpcError(INVALID_PARAMS, 'source must be a string');
  }
  const filename = typeof params.filename === 'string' ? params.filename : 'workflow.js';
  const { body } = analyse(params.source, filename);
  // A null-prototype sandbox object: from `{}` the context's global would
  // inherit the host Object.prototype, and `this.constructor.constructor`
  // would be the host Function, which reaches `process` and the real Date.
  const ctx = vm.createContext(Object.create(null));
  const install = vm.runInContext(PRELUDE, ctx);
  const hasArgs = Object.hasOwn(params, 'args');
  const l = params.limits !== null && typeof params.limits === 'object' ? params.limits : {};
  const limits = {
    maxAgents: Number.isInteger(l.maxAgents) ? l.maxAgents : 1000,
    budgetTotal: Number.isInteger(l.budgetTotal) ? l.budgetTotal : null,
    maxItems: Number.isInteger(l.maxItems) ? l.maxItems : 4096,
  };
  const verbs = Array.isArray(params.verbs) ? params.verbs.filter((v) => typeof v === 'string') : [];
  const settings = { hasArgs, limits, child: params.child === true, verbs };
  if (hasArgs) settings.args = params.args;
  // Host realm, shared by the run's contexts and held only in their install
  // closures. Every value in or out is a primitive.
  const state = { spent: Number.isInteger(params.spent) && params.spent > 0 ? params.spent : 0 };
  const hostState = function hostState(op, value) {
    switch (op) {
      case 'spent':
        return state.spent;
      case 'spend':
        if (typeof value === 'number' && value > state.spent) state.spent = value;
        return state.spent;
      default:
        return null;
    }
  };
  // Each child workflow runs in a context of its own, with its own copy of the
  // hooks over the run's shared state. Its `workflow` always throws, so
  // nesting stops at one level however the child reaches it, and no object
  // crosses between contexts: the child's args go in, and its result comes
  // back, as JSON strings. Returns the child-realm promise of the child's
  // result, or a string (a primitive) saying why the child did not start.
  const hostStart = function hostStart(source, filename, argsJson) {
    if (typeof source !== 'string' || typeof filename !== 'string') return 'workflow: bad source';
    let script;
    let child;
    try {
      const { body } = analyse(source, filename);
      script = new vm.Script("(async () => {'use strict';\n" + body + '\n})()', {
        filename,
        lineOffset: -1,
      });
      child = vm.createContext(Object.create(null));
    } catch (err) {
      return 'workflow ' + filename + ': ' + String(err && err.message ? err.message : err);
    }
    const childSettings = { hasArgs: argsJson !== null, limits, child: true, verbs: settings.verbs };
    if (argsJson !== null) {
      try {
        childSettings.args = fromScript(argsJson);
      } catch (err) {
        return 'workflow ' + filename + ': ' + err;
      }
    }
    const installChild = vm.runInContext(PRELUDE, child);
    installChild(hostCall, hostNotify, hostStart, hostState, JSON.stringify(childSettings));
    return script.runInContext(child);
  };
  install(hostCall, hostNotify, hostStart, hostState, JSON.stringify(settings));
  let script;
  try {
    script = new vm.Script("(async () => {'use strict';\n" + body + '\n})()', {
      filename,
      lineOffset: -1,
    });
  } catch (err) {
    throw new RpcError(SYNTAX_ERROR, String(err.message), { filename });
  }
  let value;
  try {
    value = await script.runInContext(ctx);
  } catch (e) {
    const { message, data } = describeThrown(e, filename);
    throw new RpcError(SCRIPT_ERROR, message, data);
  }
  let json;
  try {
    json = JSON.stringify(value);
  } catch (err) {
    throw new RpcError(
      SCRIPT_ERROR,
      `return value is not JSON-serialisable: ${err.message}`,
      { name: null, stack: null },
    );
  }
  return { value: json === undefined ? null : JSON.parse(json) };
}

async function handle(line) {
  if (line.trim() === '') return;
  let msg;
  try {
    msg = JSON.parse(line);
  } catch (err) {
    sendError(null, PARSE_ERROR, `invalid JSON: ${err.message}`);
    return;
  }
  if (msg === null || typeof msg !== 'object' || Array.isArray(msg)) {
    sendError(null, PARSE_ERROR, 'message is not a JSON object');
    return;
  }
  if (typeof msg.method !== 'string') {
    // A response to one of our requests.
    const resolve = typeof msg.id === 'number' ? pending.get(msg.id) : undefined;
    if (resolve !== undefined) {
      pending.delete(msg.id);
      resolve(msg);
    }
    return;
  }
  const id = msg.id === undefined ? null : msg.id;
  try {
    if (msg.method === 'meta') {
      const params = msg.params ?? {};
      if (typeof params.source !== 'string') {
        throw new RpcError(INVALID_PARAMS, 'source must be a string');
      }
      const filename = typeof params.filename === 'string' ? params.filename : 'workflow.js';
      const { meta } = analyse(params.source, filename);
      if (id !== null) send({ jsonrpc: '2.0', id, result: { meta } });
    } else if (msg.method === 'run') {
      if (runActive) throw new RpcError(RUN_ACTIVE, 'a run is already active');
      runActive = true;
      let result;
      try {
        result = await run(msg.params ?? {});
      } finally {
        runActive = false;
        pending.clear();
      }
      if (id !== null) send({ jsonrpc: '2.0', id, result });
    } else {
      throw new RpcError(METHOD_NOT_FOUND, `method not found: ${msg.method}`);
    }
  } catch (err) {
    if (id === null) return;
    if (err instanceof RpcError) sendError(id, err.code, err.message, err.data);
    else sendError(id, INTERNAL_ERROR, String(err && err.message ? err.message : err));
  }
}

process.on('unhandledRejection', (r) => {
  process.stderr.write(`unhandledRejection: ${r && r.stack ? r.stack : String(r)}\n`);
});

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
// Not awaited: responses to Node's own requests must be read while a `run` is pending.
rl.on('line', (line) => {
  handle(line).catch((err) => {
    process.stderr.write(`handle failed: ${err && err.stack ? err.stack : String(err)}\n`);
  });
});
// On close, do not exit: let the event loop drain so stdout writes are not truncated.
