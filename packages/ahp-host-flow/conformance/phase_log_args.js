export const meta = { name: 'phase-log-args', description: 'phase() groups calls, log() narrates, args pass verbatim', phases: [{ title: 'Map' }, { title: 'Reduce' }] }
phase('Map')
log(`mapping ${args.items.length} items`)
const mapped = await parallel(args.items.map((item) => () => agent(item, { label: `map ${item}` })))
phase('Reduce')
const reduced = await agent(mapped.join('+'), { phase: 'Custom', label: 'reduce' })
return { args, reduced }
