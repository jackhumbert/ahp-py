export const meta = { name: 'no_gates', description: 'a run with no answer path fails ask() and approve() clearly' }
const out = []
try { await ask('q') } catch (e) { out.push(e.message) }
try { await approve('s') } catch (e) { out.push(e.message) }
canvas('https://example.com/x')
return out
