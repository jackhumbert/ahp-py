export const meta = { name: 'verbs', description: 'custom verbs reach host handlers without a model' }
const found = await tool('search', { q: 'docs' })
const view = await render({ title: 'T' })
const ok = await approve('ada', 'Ship it?')
let caught = null
try { await boom(1) } catch (e) { caught = e.message }
const kinds = [typeof tool, typeof nope, typeof ask]
const a = await agent('after')
return { found, view, ok, caught, kinds, a }
