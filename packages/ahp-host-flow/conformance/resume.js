export const meta = { name: 'resume', description: 'a resumed run replays completed agent() calls from the journal and runs the rest live' }
phase('Plan')
const plan = await agent('plan')
const [x, y] = await parallel([() => agent('x', { label: 'x' }), () => agent('y', { label: 'y' })])
phase('Build')
const built = await pipeline(['p', 'q'], (item) => agent('hang build ' + item))
return { plan, x, y, built }
