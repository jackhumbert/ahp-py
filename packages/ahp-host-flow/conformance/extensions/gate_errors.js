export const meta = { name: 'gate_errors', description: 'bad ask(), approve() and canvas() arguments throw before the wire' }
const out = []
const note = (e) => out.push(e.message)
try { await ask(1) } catch (e) { note(e) }
try { await ask('q', { choices: [] }) } catch (e) { note(e) }
try { await ask('q', { choices: ['a'], schema: {} }) } catch (e) { note(e) }
try { await ask('q', { schema: 5 }) } catch (e) { note(e) }
try { await approve(2) } catch (e) { note(e) }
try { await approve('s', { diff: 5 }) } catch (e) { note(e) }
try { canvas('ftp://x') } catch (e) { note(e) }
try { canvas('https://x', { title: 3 }) } catch (e) { note(e) }
return out
