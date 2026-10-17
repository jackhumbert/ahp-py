export const meta = { name: 'parallel', description: 'parallel() is a barrier; a throwing thunk resolves to null' }
const order = []
const results = await parallel([
  // Slow: finishes several awaits after the sync thunk, and must still finish before parallel returns.
  async () => { const r = await agent('a'); await agent('a2'); await agent('a3'); order.push('a'); return r },
  () => { throw new Error('sync throw') },
  async () => { throw new Error('async throw') },
  () => agent('fail b'),
  () => { order.push('sync'); return 42 },
])
order.push('after')
let rejected = null
try { await parallel(new Array(4097).fill(() => 1)) } catch (e) { rejected = e.message }
const empty = await parallel([])
return { results, order, rejected, empty }
