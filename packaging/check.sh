#!/usr/bin/env bash
# Check a resources folder the way the packaged app uses it (ARCHITECTURE.md §10): the one
# packaging/resources.sh assembles, or Ontological.app/Contents/Resources itself.
#
#   packaging/check.sh <resources> [--smoke]
#
# Every Postgres file must still carry its hardened-runtime signature, and every executable the
# disable-library-validation entitlement (electron-builder must not re-sign the tree). The
# model's files must match their pinned SHA-256s, and the DuckDB scanners must load from
# duckdb-extensions/ with autoinstall off. Nothing here uses the network.
#
# --smoke also runs S4's control.sh and smoke.sh on the tree. Library validation is enforced only
# with SIP on, so run it on a Mac with SIP on after changing how the tree is built, signed or
# packaged: GitHub's runners have SIP off, where control.sh skips.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
res="$(cd "$1" && pwd)"
smoke="${2:-}"
engine_py() { uv run --quiet --project "$repo/engine" --locked --offline python "$@"; }

for f in "$res"/postgres/lib/*.dylib "$res"/postgres/bin/*; do
  codesign --verify --strict "$f" 2>/dev/null || { echo "bad signature: $f" >&2; exit 1; }
  info="$(codesign --display --verbose=1 "$f" 2>&1)"
  grep -Eq 'flags=0x[0-9a-f]+\([a-z,-]*runtime' <<<"$info" || { echo "no hardened runtime: $f" >&2; exit 1; }
done
for f in "$res"/postgres/bin/*; do
  entitlements="$(codesign --display --entitlements - --xml "$f" 2>/dev/null)"
  grep -q 'com.apple.security.cs.disable-library-validation' <<<"$entitlements" ||
    { echo "no disable-library-validation entitlement: $f" >&2; exit 1; }
done
echo "postgres: $(find "$res/postgres" -type f | wc -l | tr -d ' ') files, signatures and entitlements intact"

engine_py -c '
import sys
from pathlib import Path
from ontological.embed import model
from ontological.sources import extensions
model.verify(Path(sys.argv[1]))
print(f"model: {model.MODEL_ID} matches its pins")
extensions.verify(Path(sys.argv[2]))
print(f"duckdb-extensions: {", ".join(extensions.INSTALLED)} load with autoinstall off")
' "$res/model" "$res/duckdb-extensions"

if [[ "$smoke" == --smoke ]]; then
  "$repo/tools/spikes/s4_postgres/control.sh" "$res/postgres"
  "$repo/tools/spikes/s4_postgres/smoke.sh" "$res/postgres"
fi
