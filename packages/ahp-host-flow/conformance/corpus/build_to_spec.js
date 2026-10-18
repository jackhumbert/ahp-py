export const meta = {
  name: 'build_to_spec',
  description: 'Plan a change from a written spec, build it step by step, and review each step',
  whenToUse: 'Implement a spec as small, reviewed steps',
  phases: [{ title: 'Plan' }, { title: 'Build' }, { title: 'Review' }],
}

const STEPS = {
  type: 'object',
  required: ['steps'],
  properties: {
    steps: {
      type: 'array',
      minItems: 1,
      items: {
        type: 'object',
        required: ['title', 'files'],
        properties: { title: { type: 'string' }, files: { type: 'array', items: { type: 'string' } } },
      },
    },
  },
}
const BUILT = {
  type: 'object',
  required: ['step', 'changed'],
  properties: { step: { type: 'string' }, changed: { type: 'array', items: { type: 'string' } } },
}
const VERDICT = {
  type: 'object',
  required: ['step', 'verdict'],
  properties: {
    step: { type: 'string' },
    verdict: { type: 'string', enum: ['approve', 'changes'] },
    notes: { type: 'string' },
  },
}
const SUMMARY = {
  type: 'object',
  required: ['approved', 'summary'],
  properties: { approved: { type: 'boolean' }, summary: { type: 'string' } },
}

const spec = (args && args.spec) || 'docs/spec.md'

phase('Plan')
const plan = await agent(
  `Plan: read the spec at ${spec} and split it into small, ordered steps, each naming the files it touches.`,
  { label: 'plan', schema: STEPS, model: 'opus' },
)
if (!plan) return { ok: false, reason: 'no plan' }
log(`${plan.steps.length} steps planned`)

phase('Build')
const reviewed = await pipeline(
  plan.steps,
  (step) =>
    agent(`Build: implement the step "${step.title}".\nFiles: ${step.files.join(', ')}`, {
      label: `build: ${step.title}`,
      schema: BUILT,
      model: 'sonnet',
    }),
  (built, step) =>
    built &&
    agent(`Review step: "${step.title}" changed ${built.changed.join(', ')}. Approve it or ask for changes.`, {
      label: `review: ${step.title}`,
      schema: VERDICT,
      model: 'sonnet',
    }),
)
const approved = reviewed.filter((v) => v && v.verdict === 'approve').length
log(`${approved}/${plan.steps.length} steps approved`)

phase('Review')
const verdict = await agent(
  `Review change: ${approved}/${plan.steps.length} steps approved. Verdicts: ${JSON.stringify(reviewed)}. Is the change ready to land?`,
  { label: 'final review', schema: SUMMARY, model: 'opus' },
)

return {
  ok: Boolean(verdict && verdict.approved),
  steps: plan.steps.map((s) => s.title),
  verdicts: reviewed.map((v) => v && v.verdict),
  summary: verdict && verdict.summary,
}
