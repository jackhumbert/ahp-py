export const meta = { name: 'fanout', description: 'fan one question out to several agents, then reduce', phases: [{ title: 'Fan out' }, { title: 'Reduce' }] }
const topics = (args && args.topics) || ['alpha', 'beta', 'gamma']
phase('Fan out')
const answers = await parallel(topics.map((t) => () => agent(`Reply with exactly one word: ${t}`, { label: `fan-${t}` })))
phase('Reduce')
const summary = await agent(`Reply with exactly one word: done (${answers.filter(Boolean).length} answers)`, { label: 'reduce' })
return { answers, summary }
