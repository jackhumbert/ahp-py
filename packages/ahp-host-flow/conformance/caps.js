export const meta = { name: 'caps', description: '1000 agent() calls per run; 4096 items per call' }
const many = await parallel(Array.from({ length: 1000 }, (_, i) => () => agent(`a${i}`)))
let over = null
try { await agent('one too many') } catch (e) { over = e.message }
const items = await parallel(new Array(4096).fill(() => 1))
return { ok: many.filter((r, i) => r === `a${i}`).length, over, items: items.length }
