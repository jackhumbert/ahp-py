export const meta = {name: 'dual_target', description: 'runs under Claude Code and ahp-host-flow'}

// flow-shim: lets a workflow that uses ahp-host-flow's ask()/approve()/canvas()
// also run under Claude Code, which leaves unknown globals undefined and
// ignores opts.x (plan section 3 probe). Paste it right after
// `export const meta = {...}`. Under ahp-host-flow the real functions are
// kept, and agent() and opts.x are never touched.
// Set askDefault to a value to return it instead of asking an agent, and
// approveDefault to true/false likewise; false is the safe choice for
// destructive steps.
const FLOW_SHIM = { askDefault: undefined, approveDefault: undefined };
// Reading globalThis avoids the temporal dead zone that `typeof ask` would hit
// inside `const ask`.
const ask = typeof globalThis.ask === 'function' ? globalThis.ask : async function ask(question, opts) { const o = opts ?? {}; if (FLOW_SHIM.askDefault !== undefined) return FLOW_SHIM.askDefault; let schema; let pick; if (Array.isArray(o.choices)) { schema = {type: 'object', properties: {answer: {type: 'string', enum: o.choices}}, required: ['answer']}; pick = (r) => r.answer; } else if (o.schema !== null && typeof o.schema === 'object') { schema = o.schema; pick = (r) => r; } else { schema = {type: 'object', properties: {answer: {type: 'string'}}, required: ['answer']}; pick = (r) => r.answer; } const r = await agent('A workflow needs an answer from its user before it continues. Answer as the user would, with only the answer.\n\nQuestion: ' + question + (Array.isArray(o.choices) ? '\nChoices: ' + o.choices.join(', ') : ''), {label: 'ask', schema}); return r == null ? null : pick(r); };
const approve = typeof globalThis.approve === 'function' ? globalThis.approve : async function approve(summary, opts) { if (FLOW_SHIM.approveDefault !== undefined) return FLOW_SHIM.approveDefault === true; const o = opts ?? {}; const r = await agent('A workflow asks for approval before a step that changes things. Approve only if it is clearly safe and intended.\n\n' + summary + (typeof o.diff === 'string' ? '\n\n' + o.diff : ''), {label: 'approve', schema: {type: 'object', properties: {approved: {type: 'boolean'}}, required: ['approved']}}); return r != null && r.approved === true; };
const canvas = typeof globalThis.canvas === 'function' ? globalThis.canvas : function canvas() {};
const plan = await agent('plan the change', {label: 'plan', x: {chat: 'planner'}})
const go = await ask('Proceed with the plan?', {choices: ['yes', 'no']})
if (go !== 'yes') return {plan, go, ok: null, done: null}
const ok = await approve('Apply the plan', {diff: '--- a/x\n+++ b/x\n'})
canvas('https://example.com/run')
const done = ok ? await agent('apply the plan', {label: 'apply', x: {chat: 'planner'}}) : null
return {plan, go, ok, done}
