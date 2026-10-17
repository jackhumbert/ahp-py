export const meta = { name: 'budget', description: 'budget is a hard ceiling summed from output tokens' }
const before = [budget.total, budget.spent(), budget.remaining()]
const a = await agent('a')
const b = await agent('b')
const mid = [budget.spent(), budget.remaining()]
const c = await agent('c')
let thrown = null
try { await agent('d') } catch (e) { thrown = e.message }
return { before, mid, after: [budget.spent(), budget.remaining()], results: [a, b, c], thrown }
