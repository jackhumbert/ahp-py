// Drives the REAL published Microsoft TypeScript client against our host.
//
// The fixture corpora prove our reducers match the reference implementation over
// recorded inputs. This proves a first-party client can actually talk to us over
// a real socket -- which is a different claim, and the one the project exists to
// support.
//
// Emits one JSON object on stdout so the Python side can assert on it.
import { AhpClient } from '@microsoft/agent-host-protocol/client'
import { WebSocketTransport } from '@microsoft/agent-host-protocol/ws'
import { SUPPORTED_PROTOCOL_VERSIONS, chatReducer } from '@microsoft/agent-host-protocol'
import { WebSocket } from 'ws'

globalThis.WebSocket ??= WebSocket

const url = process.argv[2]
if (!url) {
  console.error('usage: driver.mjs <ws-url>')
  process.exit(2)
}

const ROOT = 'ahp-root://'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const out = { clientSupports: SUPPORTED_PROTOCOL_VERSIONS, errors: [] }

try {
  const transport = await WebSocketTransport.connect(url)
  const client = new AhpClient(transport, { requestTimeoutMs: 5000 })
  client.connect()

  const clientId = 'interop-client'
  const init = await client.initialize({
    clientId,
    protocolVersions: SUPPORTED_PROTOCOL_VERSIONS,
    initialSubscriptions: [ROOT],
  })
  out.negotiatedVersion = init.protocolVersion
  out.serverInfo = init.serverInfo
  out.rootSnapshotResource = init.snapshots?.[0]?.resource
  out.agents = init.snapshots?.[0]?.state?.agents?.map((a) => a.provider)
  // `snapshots` must be an array: the runtime calls .find() on it unguarded.
  out.snapshotsIsArray = Array.isArray(init.snapshots)

  const list = await client.request('listSessions', { channel: ROOT })
  out.listSessionsHasItems = Array.isArray(list.items)

  const sessionUri = `ahp-session:/${crypto.randomUUID()}`
  await client.request('createSession', { channel: sessionUri, provider: 'echo' })

  const rootSub = client.attachSubscription(ROOT)
  const rootEvents = []
  ;(async () => {
    for await (const ev of rootSub) rootEvents.push(ev)
  })().catch(() => {})

  await sleep(300)

  const { result: sessionSub, subscription: sessionEvents } = await client.subscribe(sessionUri)
  out.sessionSnapshot = sessionSub.snapshot?.state
  const sessionSeen = []
  ;(async () => {
    for await (const ev of sessionEvents) sessionSeen.push(ev)
  })().catch(() => {})

  await sleep(200)

  const chatUri = sessionSub.snapshot?.state?.defaultChat
    ?? sessionSub.snapshot?.state?.chats?.[0]?.resource
  out.chatUri = chatUri
  if (!chatUri) throw new Error('host published no chat for the session')

  const { result: chatSub, subscription: chatEvents } = await client.subscribe(chatUri)
  out.chatSnapshotFromSeq = chatSub.snapshot?.fromSeq

  // Feed the host's own action stream through the OFFICIAL reducers, then diff
  // against a fresh snapshot from the host. This is the strongest check
  // available: reducer equivalence over live traffic, not recorded fixtures.
  let mirrored = chatSub.snapshot.state
  const seqs = []
  ;(async () => {
    for await (const ev of chatEvents) {
      if (ev.type !== 'action') continue
      seqs.push(ev.params.serverSeq)
      mirrored = chatReducer(mirrored, ev.params.action)
    }
  })().catch(() => {})

  const turnId = crypto.randomUUID()
  client.dispatch(chatUri, {
    type: 'chat/turnStarted',
    turnId,
    startedAt: new Date().toISOString(),
    message: { text: 'hello from the interop driver', origin: { kind: 'user' } },
  })

  await sleep(700)

  out.chatActionSeqs = seqs
  out.seqsAreMonotonic = seqs.every((s, i) => i === 0 || s > seqs[i - 1])

  // The echo of our own dispatch must carry an origin the host stamped, since
  // dispatchAction never sent one.
  const echoed = []
  for (const ev of sessionSeen) if (ev.type === 'action') echoed.push(ev.params)

  const fresh = await client.request('subscribe', { channel: chatUri })
  out.hostState = fresh.snapshot?.state
  out.mirroredState = mirrored

  // Where the two disagree, and on which top-level keys. `modifiedAt` is
  // EXPECTED to differ: the chat reducer stamps it from the wall clock in six
  // places, so host and client provably cannot compute bit-identical state.
  // Any OTHER differing key is a real divergence.
  const ours = sortKeys(stripNulls(fresh.snapshot?.state ?? {}))
  const theirs = sortKeys(stripNulls(mirrored ?? {}))
  out.divergentKeys = [...new Set([...Object.keys(ours), ...Object.keys(theirs)])]
    .filter((k) => JSON.stringify(ours[k]) !== JSON.stringify(theirs[k]))
    .sort()
  out.reducerAgreement = out.divergentKeys.length === 0
  out.reducerAgreementIgnoringClock = out.divergentKeys.every((k) => k === 'modifiedAt')

  out.turns = (fresh.snapshot?.state?.turns ?? []).map((t) => ({
    id: t.id,
    state: t.state,
    text: (t.responseParts ?? []).filter((p) => p.kind === 'markdown').map((p) => p.content).join(''),
  }))

  await client.ping()
  out.pingOk = true

  await client.shutdown()
  out.ok = true
} catch (err) {
  out.ok = false
  out.errors.push(`${err?.constructor?.name}: ${err?.message}`)
}

function stripNulls(v) {
  if (Array.isArray(v)) return v.map(stripNulls)
  if (v && typeof v === 'object') {
    const o = {}
    for (const [k, x] of Object.entries(v)) if (x !== null && x !== undefined) o[k] = stripNulls(x)
    return o
  }
  return v
}
function sortKeys(v) {
  if (Array.isArray(v)) return v.map(sortKeys)
  if (v && typeof v === 'object') {
    return Object.fromEntries(Object.keys(v).sort().map((k) => [k, sortKeys(v[k])]))
  }
  return v
}

process.stdout.write(JSON.stringify(out))
process.exit(out.ok ? 0 : 1)
