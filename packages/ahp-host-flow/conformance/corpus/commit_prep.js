export const meta = {
  name: 'commit_prep',
  description: 'Lint a pending change against house rules and draft its commit message',
  whenToUse: 'Before committing',
  phases: [{ title: 'Lint' }, { title: 'Message' }],
}

const RULES = ['no debug prints', 'no personal paths', 'docs updated with code']
const LINT = {
  type: 'object',
  required: ['path', 'violations'],
  properties: {
    path: { type: 'string' },
    violations: {
      type: 'array',
      items: {
        type: 'object',
        required: ['rule', 'note'],
        properties: { rule: { type: 'string' }, note: { type: 'string' } },
      },
    },
  },
}
const MESSAGE = {
  type: 'object',
  required: ['subject', 'body'],
  properties: { subject: { type: 'string', maxLength: 72 }, body: { type: 'string' } },
}

const paths = (args && args.paths) || ['src/app.py', 'README.md']

phase('Lint')
const linted = await parallel(
  paths.map((p) => () =>
    agent(`Lint: ${p}\nCheck this file's pending change against these rules: ${RULES.join('; ')}.`, {
      label: `lint ${p}`,
      schema: LINT,
      model: 'haiku',
    }),
  ),
)
const violations = linted.filter(Boolean).flatMap((l) => l.violations.map((v) => ({ path: l.path, ...v })))
log(violations.length ? `${violations.length} violation(s)` : 'clean')

phase('Message')
const message = await agent(
  `Commit message: draft a conventional commit message for the pending change.\nPaths: ${paths.join(', ')}\nViolations: ${violations.length}`,
  { label: 'message', schema: MESSAGE, model: 'sonnet' },
)

return {
  clean: violations.length === 0,
  violations,
  message: message ? `${message.subject}\n\n${message.body}` : null,
}
