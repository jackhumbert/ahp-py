export const meta = { name: 'workflow_sources', description: 'workflow() takes a source-qualified name' }
const host = await workflow('host:child', { who: 'host' })
let workspace = null
try { await workflow('workspace:child') } catch (e) { workspace = e.message }
let bogus = null
try { await workflow('bogus:child') } catch (e) { bogus = e.message }
return { host, workspace, bogus }
