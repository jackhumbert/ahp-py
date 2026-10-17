export const meta = { name: 'child', description: 'a child workflow' }
const reply = await agent(`child ${args.who}`)
return { reply, sawArgs: args }
