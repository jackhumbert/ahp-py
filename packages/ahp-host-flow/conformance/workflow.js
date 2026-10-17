export const meta = { name: 'workflow', description: 'workflow() runs a child inline, sharing caps and budget; one level only' }
const byName = await workflow('child', { who: 'name' })
const byPath = await workflow({ scriptPath: 'workflows/child.js' }, { who: 'path' })
let nested = null
try { await workflow('nested') } catch (e) { nested = e.message }
let unknown = null
try { await workflow('no-such-workflow') } catch (e) { unknown = e.message }
let broken = null
try { await workflow('broken') } catch (e) { broken = e.message.startsWith('workflow ') }
const after = await agent('parent')
return { byName, byPath, nested, unknown, broken, after, args: typeof args }
