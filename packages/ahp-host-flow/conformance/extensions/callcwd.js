export const meta = { name: 'callcwd', description: 'opts.x.cwd reaches the dispatcher unchanged' }
const a = await agent('a', {x: {cwd: 'sub'}})
const b = await agent('b', {x: {cwd: '/srv/project'}})
const c = await agent('c')
return [a, b, c]
