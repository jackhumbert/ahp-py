// The pinned sources are .ts but import each other with .js specifiers (NodeNext
// style). Rewrite those back so `--experimental-transform-types` can load the
// pinned tree directly, rather than falling back to an older published build.
import { existsSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';

export async function resolve(specifier, context, next) {
  if (specifier.endsWith('.js') && context.parentURL?.includes('/agent-host-protocol/types/')) {
    const candidate = new URL(specifier, context.parentURL);
    const asTs = candidate.href.replace(/\.js$/, '.ts');
    if (existsSync(fileURLToPath(asTs))) {
      return next(pathToFileURL(fileURLToPath(asTs)).href, context);
    }
  }
  return next(specifier, context);
}
