export const meta = { name: 'nested', description: 'calls workflow() from a child' }
return await workflow('child', { who: 'deep' })
