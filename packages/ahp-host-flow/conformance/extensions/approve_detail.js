export const meta = { name: 'approve_detail', description: 'approve() with x.detail gives {approved, reason, answeredBy}' }
const plain = await approve('Ship?')
const rich = await approve('Drop?', { x: { detail: true } })
const ok = await approve('Ship?', { x: { detail: true } })
let bad = null
try { await approve('Bad?', { x: 1 }) } catch (e) { bad = e.message }
return { plain, rich, ok, bad }
