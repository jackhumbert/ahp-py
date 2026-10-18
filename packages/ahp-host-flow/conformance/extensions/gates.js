export const meta = { name: 'gates', description: 'ask(), approve() and canvas() reach the host as gates' }
const a = await agent('first')
const yes = await ask('Ship?', { choices: ['yes', 'no'] })
const name = await ask('Name?')
const cfg = await ask('Config?', { schema: { type: 'object', properties: { n: { type: 'integer' } }, required: ['n'] } })
const bad = await ask('Pick?', { choices: ['a', 'b'] })
const ok = await approve('Delete?', { diff: '-x\n+y\n' })
const no = await approve('Drop table?')
canvas('https://example.com/view', { title: 'View' })
const b = await agent('second')
return { a, yes, name, cfg, bad, ok, no, b }
