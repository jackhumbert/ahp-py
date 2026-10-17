export const meta = { name: 'caps-concurrency', description: 'at most the configured number of agents run at once' }
return await parallel(Array.from({ length: 12 }, (_, i) => () => agent(`c${i}`)))
