export const meta = {
  name: 'fanout',
  description: 'Ask several agents the same question about different topics, then reduce their answers to one',
  whenToUse: 'Compare independent answers before deciding',
  phases: [{ title: 'Fan out' }, { title: 'Reduce' }],
}

const ANSWER = {
  type: 'object',
  required: ['topic', 'answer', 'confidence'],
  properties: {
    topic: { type: 'string' },
    answer: { type: 'string' },
    confidence: { type: 'number', minimum: 0, maximum: 1 },
  },
}
const REDUCED = {
  type: 'object',
  required: ['best', 'summary'],
  properties: { best: { type: 'string' }, summary: { type: 'string' } },
}

const question = (args && args.question) || 'Which option should we pick?'
const topics = (args && args.topics) || ['alpha', 'beta', 'gamma']

phase('Fan out')
const answers = await parallel(
  topics.map((t) => () =>
    agent(`Answer: ${question}\nTopic: ${t}`, { label: `fan-${t}`, schema: ANSWER, model: 'haiku' }),
  ),
)
const usable = answers.filter(Boolean)
log(`${usable.length}/${topics.length} answers`)

phase('Reduce')
const reduced = await agent(`Reduce: pick the best answer.\nAnswers: ${JSON.stringify(usable)}`, {
  label: 'reduce',
  schema: REDUCED,
  model: 'sonnet',
})

return {
  answers: usable.map((a) => a.topic),
  best: reduced && reduced.best,
  summary: reduced && reduced.summary,
}
