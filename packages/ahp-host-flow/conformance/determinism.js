export const meta = { name: 'determinism', description: 'non-deterministic APIs throw; explicit dates work' }
const tries = {
  'Date.now()': () => Date.now(),
  'Math.random()': () => Math.random(),
  'new Date()': () => new Date(),
  'Date()': () => Date(),
  'Intl format()': () => new Intl.DateTimeFormat('en-US').format(),
  'Intl formatToParts()': () => new Intl.DateTimeFormat('en-US').formatToParts(),
}
const banned = {}
for (const [k, f] of Object.entries(tries)) {
  try { f(); banned[k] = false } catch (e) { banned[k] = e.message.includes('is not allowed in a workflow script') }
}
return {
  banned,
  explicit: new Date(0).toISOString(),
  formatted: new Intl.DateTimeFormat('en-US', { timeZone: 'UTC' }).format(new Date(0)),
  apis: [typeof require, typeof process, typeof console, typeof setTimeout],
}
