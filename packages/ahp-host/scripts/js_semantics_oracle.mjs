// Feed a JSONL stream of {reducer, initial, actions} through the REAL upstream
// TypeScript reducers and print the resulting state, so the Python port can be
// diffed against it on inputs the fixture corpus never covers.
//
// The corpus comparator normalises `null` away on both sides, so it is
// structurally incapable of catching an undefined-vs-null divergence. This is.
import { createInterface } from 'node:readline';

const T = '/Users/me/Github/agent-host-server-py/.research/agent-host-protocol/types';

const { terminalReducer } = await import(`${T}/channels-terminal/reducer.ts`);
const { changesetReducer } = await import(`${T}/channels-changeset/reducer.ts`);
const { annotationsReducer } = await import(`${T}/channels-annotations/reducer.ts`);
const { sessionReducer } = await import(`${T}/channels-session/reducer.ts`);
const { chatReducer } = await import(`${T}/channels-chat/reducer.ts`);
const { rootReducer } = await import(`${T}/channels-root/reducer.ts`);

const REDUCERS = {
  terminal: terminalReducer,
  changeset: changesetReducer,
  annotations: annotationsReducer,
  session: sessionReducer,
  chat: chatReducer,
  root: rootReducer,
};

// The reducers are not pure: chatReducer stamps modifiedAt from the clock.
// Pin it exactly as the conformance corpora do.
Date.now = () => 9999;

const rl = createInterface({ input: process.stdin });
for await (const line of rl) {
  if (!line.trim()) continue;
  const testCase = JSON.parse(line);
  const reducer = REDUCERS[testCase.reducer];
  let out;
  try {
    let state = testCase.initial;
    for (const action of testCase.actions) state = reducer(state, action, () => {});
    // JSON.stringify is the point: it is what drops `undefined`-valued keys,
    // which is the whole distinction under test.
    out = { name: testCase.name, ok: true, state: JSON.parse(JSON.stringify(state)) };
  } catch (error) {
    out = { name: testCase.name, ok: false, error: String(error && error.message) };
  }
  process.stdout.write(JSON.stringify(out) + '\n');
}
