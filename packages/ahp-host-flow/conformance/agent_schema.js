export const meta = { name: 'agent_schema', description: 'agent() re-validates a result against opts.schema' }
const ok = await agent('fine', { schema: { type: 'string' } })
const bad = await agent('not an object', { schema: { type: 'object' } })
const ignored = await agent('no schema object', { schema: 'not a schema' })
return { ok, bad, ignored }
