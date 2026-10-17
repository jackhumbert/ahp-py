export const meta = { name: 'agent', description: 'agent() returns text, or null on skip or error' }
const text = await agent('hello', { label: 'greet', x: { provider: 'any' }, unknownKey: 1 })
const skipped = await agent('skip me')
const failed = await agent('fail me')
return { text, skipped, failed }
