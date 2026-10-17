export const meta = { name: 'pipeline', description: 'pipeline() has no barrier; a stage gets (prev, item, index); a throw drops the item' }
const seen = []
const out = await pipeline(['x', 'y', 'z'],
  async (prev, item, index) => {
    seen.push(['s1', prev, item, index])
    if (item === 'y') throw new Error('drop')
    return agent(prev + '1')
  },
  async (prev, item, index) => { seen.push(['s2', prev, item, index]); return prev + '2' })
const s1 = seen.filter((s) => s[0] === 's1').sort((a, b) => a[3] - b[3])
const s2 = seen.filter((s) => s[0] === 's2').sort((a, b) => a[3] - b[3])
let rejected = null
try { await pipeline(new Array(4097).fill(0), (p) => p) } catch (e) { rejected = e.message }
const passthrough = await pipeline([1, 2])
return { out, s1, s2, rejected, passthrough }
