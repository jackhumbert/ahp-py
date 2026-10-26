#!/usr/bin/env bash
# Vendor the pinned upstream conformance corpora and schemas into vendor/upstream/.
#
# The corpora exist ONLY in the upstream git repository -- they are not published
# as release assets -- so they are committed here and never fetched at test time.
# The pin lives in UPSTREAM.md; change it there and re-run this script.
set -euo pipefail

UPSTREAM_REPO="https://github.com/microsoft/agent-host-protocol.git"
# A tag (`spec/vX.Y.Z`) or a branch (`main`). A tag must still resolve to the
# commit below; a branch moves, so the commit must merely be on it. Either way
# the archive is taken from the COMMIT, never the ref.
SPEC_REF="main"
SPEC_COMMIT="c5628cd4b09f76cf143ebe655e872c0fb3f1b8d8"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECKOUT="$ROOT/.research/agent-host-protocol"
DEST="$ROOT/vendor/upstream"

if [ ! -d "$CHECKOUT/.git" ]; then
  echo "==> cloning upstream into .research/ (git-ignored)"
  mkdir -p "$ROOT/.research"
  git clone --quiet "$UPSTREAM_REPO" "$CHECKOUT"
fi

echo "==> fetching branches and tags"
git -C "$CHECKOUT" fetch --quiet --tags origin

if git -C "$CHECKOUT" rev-parse --quiet --verify "refs/tags/$SPEC_REF" >/dev/null; then
  actual="$(git -C "$CHECKOUT" rev-parse "$SPEC_REF^{commit}")"
  if [ "$actual" != "$SPEC_COMMIT" ]; then
    echo "ERROR: $SPEC_REF resolves to $actual, but UPSTREAM.md pins $SPEC_COMMIT" >&2
    echo "       Either the tag moved or the pin is stale. Resolve before vendoring." >&2
    exit 1
  fi
elif ! git -C "$CHECKOUT" merge-base --is-ancestor "$SPEC_COMMIT" "origin/$SPEC_REF" 2>/dev/null; then
  echo "ERROR: $SPEC_COMMIT is not on upstream $SPEC_REF" >&2
  echo "       The branch was rewritten or the pin is wrong. Resolve before vendoring." >&2
  exit 1
fi

echo "==> vendoring $SPEC_REF ($SPEC_COMMIT)"
rm -rf "$DEST"
mkdir -p "$DEST"
git -C "$CHECKOUT" archive "$SPEC_COMMIT" \
  types/test-cases \
  types/version/registry.ts \
  types/action-origin.generated.ts \
  types/common/actions.ts \
  types/common/errors.ts \
  types/common/messages.ts \
  types/common/commands.ts \
  types/channels-root/commands.ts \
  types/channels-session/commands.ts \
  types/channels-chat/commands.ts \
  types/channels-terminal/commands.ts \
  types/channels-changeset/commands.ts \
  types/channels-resource-watch/commands.ts \
  types/channels-automation/commands.ts \
  types/channels-tcp/commands.ts \
  types/channels-session/state.ts \
  schema \
  | tar -x -C "$DEST"

# Flatten: vendor/upstream/{test-cases,ts,schema}
mv "$DEST/types/test-cases" "$DEST/test-cases"
mkdir -p "$DEST/ts"
mv "$DEST/types/version/registry.ts"        "$DEST/ts/registry.ts"
mv "$DEST/types/action-origin.generated.ts" "$DEST/ts/action-origin.generated.ts"
mv "$DEST/types/common/actions.ts"          "$DEST/ts/actions.ts"
mv "$DEST/types/common/errors.ts"           "$DEST/ts/errors.ts"
# messages.ts carries CommandMap / ServerCommandMap and the notification maps --
# the authority for "how many commands are there, in which direction". A peer's
# parity matrix is derived from it rather than from a hand-kept list.
mv "$DEST/types/common/messages.ts"        "$DEST/ts/messages.ts"
# Every `*Params` interface, so a peer can derive which commands are pinned to
# `ahp-root://` instead of remembering. Seventeen of twenty-seven are, and the
# ten that are not include `completions` -- forcing that one to root silently
# breaks every @-mention picker.
mv "$DEST/types/common/commands.ts"        "$DEST/ts/commands.ts"
for ch in root session chat terminal changeset resource-watch automation tcp; do
  mv "$DEST/types/channels-$ch/commands.ts" "$DEST/ts/commands-$ch.ts"
done
mv "$DEST/types/channels-session/state.ts"  "$DEST/ts/session-state.ts"
rm -rf "$DEST/types"

cat > "$DEST/PIN.json" <<EOF
{
  "upstream": "microsoft/agent-host-protocol",
  "specRef": "$SPEC_REF",
  "specCommit": "$SPEC_COMMIT",
  "vendored": ["test-cases/reducers", "test-cases/round-trips", "test-cases/version-negotiation.json", "schema", "ts"]
}
EOF

echo "==> vendored:"
echo "    reducer fixtures:    $(find "$DEST/test-cases/reducers" -name '*.json' | wc -l | tr -d ' ')"
echo "    round-trip fixtures: $(find "$DEST/test-cases/round-trips" -name '*.json' | wc -l | tr -d ' ')"
echo "    schemas:             $(find "$DEST/schema" -name '*.json' | wc -l | tr -d ' ')"
