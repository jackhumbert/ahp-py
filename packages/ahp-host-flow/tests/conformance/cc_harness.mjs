// A minimal Claude-Code-like harness: `node cc_harness.mjs <script> <answers.json>`.
// It exposes only agent/parallel/pipeline/phase/log/args/budget/workflow (no
// ask/approve/canvas, no opts.x handling) and prints one JSON line.
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

try {
  const source = await readFile(process.argv[2], 'utf8');
  const answers = JSON.parse(await readFile(process.argv[3], 'utf8'));
  const body = source.replace(/^export const meta\b/m, 'const meta');
  const calls = [];
  const logs = [];
  const sandbox = Object.create(null);
  sandbox.agent = async (prompt, opts) => {
    const o = JSON.parse(JSON.stringify(opts ?? {}));
    calls.push({ prompt, opts: o });
    const byLabel = answers.by_label ?? {};
    const byPrompt = answers.by_prompt ?? {};
    if (typeof o.label === 'string' && Object.hasOwn(byLabel, o.label)) return byLabel[o.label];
    if (Object.hasOwn(byPrompt, prompt)) return byPrompt[prompt];
    return null;
  };
  sandbox.parallel = (thunks) =>
    Promise.all(thunks.map((t) => Promise.resolve().then(t).catch(() => null)));
  sandbox.pipeline = (items, ...stages) =>
    Promise.all(
      items.map((item, i) =>
        stages
          .reduce((p, s) => p.then((prev) => s(prev, item, i)), Promise.resolve(item))
          .catch(() => null),
      ),
    );
  sandbox.phase = (t) => {
    logs.push({ phase: t });
  };
  sandbox.log = (m) => {
    logs.push({ log: m });
  };
  sandbox.args = answers.args;
  sandbox.budget = { total: null, spent: () => 0, remaining: () => Infinity };
  sandbox.workflow = () => {
    throw new Error('workflow() is not emulated');
  };
  vm.createContext(sandbox);
  const value = await new vm.Script("(async () => {'use strict';\n" + body + "\n})()").runInContext(
    sandbox,
  );
  console.log(JSON.stringify({ ok: true, value, calls, logs }));
} catch (e) {
  console.log(JSON.stringify({ ok: false, error: String((e && e.message) || e) }));
}
