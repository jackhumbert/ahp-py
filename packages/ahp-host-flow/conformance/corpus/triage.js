export const meta = {
  name: 'triage',
  description: 'Diagnose failing tests one by one and group them by cause',
  whenToUse: 'A test suite is red and you want a per-test diagnosis',
  phases: [{ title: 'Collect' }, { title: 'Diagnose' }, { title: 'Report' }],
}

const FAILURES = {
  type: 'object',
  required: ['failures'],
  properties: { failures: { type: 'array', items: { type: 'string' } } },
}
const DIAGNOSIS = {
  type: 'object',
  required: ['test', 'kind', 'cause'],
  properties: {
    test: { type: 'string' },
    kind: { type: 'string', enum: ['product', 'test', 'flaky'] },
    cause: { type: 'string' },
  },
}
const REPORT = {
  type: 'object',
  required: ['groups'],
  properties: {
    groups: {
      type: 'array',
      items: {
        type: 'object',
        required: ['cause', 'tests'],
        properties: { cause: { type: 'string' }, tests: { type: 'array', items: { type: 'string' } } },
      },
    },
  },
}

phase('Collect')
let failures = args && args.failures
if (!failures) {
  const listed = await agent('List failing tests: run the test suite and list the id of every failing test.', {
    label: 'collect',
    schema: FAILURES,
  })
  failures = listed ? listed.failures : []
}
if (failures.length === 0) return { failures: 0, kinds: [], groups: [] }
log(`${failures.length} failing tests`)

phase('Diagnose')
const diagnoses = await parallel(
  failures.map((id) => () =>
    agent(
      `Diagnose: ${id}\nRead the test and the code it covers; say whether the product, the test or flakiness is to blame.`,
      { label: `diagnose ${id}`, schema: DIAGNOSIS },
    ),
  ),
)

phase('Report')
const found = diagnoses.filter(Boolean)
const report = await agent(`Report: group these diagnoses by cause.\nDiagnoses: ${JSON.stringify(found)}`, {
  label: 'report',
  schema: REPORT,
})

return {
  failures: failures.length,
  kinds: found.map((d) => d.kind),
  groups: report ? report.groups : [],
}
