#!/usr/bin/env bash
# Sign every Mach-O file in a trim.sh tree with the hardened runtime. Libraries first, then
# executables, which get the entitlements. Ad-hoc ("-") until the Apple Developer account
# exists; set CODESIGN_IDENTITY to a Developer ID to sign for real.
#
#   sign.sh <dist> [entitlements.plist]
set -euo pipefail

dist="$(cd "$1" && pwd)"
entitlements="${2:-}"
identity="${CODESIGN_IDENTITY:--}"

timestamp=--timestamp
[[ "$identity" == "-" ]] && timestamp=--timestamp=none

for f in "$dist"/lib/*.dylib; do
  codesign --force --sign "$identity" --options runtime "$timestamp" "$f" 2>/dev/null
done
for f in "$dist"/bin/*; do
  codesign --force --sign "$identity" --options runtime "$timestamp" \
    ${entitlements:+--entitlements "$entitlements"} "$f" 2>/dev/null
done

for f in "$dist"/lib/*.dylib "$dist"/bin/*; do
  codesign --verify --strict "$f"
  # The hardened runtime shows as flags=0x10000(runtime), or 0x10002(adhoc,runtime).
  info="$(codesign --display --verbose=1 "$f" 2>&1)"
  grep -Eq 'flags=0x[0-9a-f]+\([a-z,-]*runtime' <<<"$info" ||
    { echo "no hardened runtime on $f" >&2; exit 1; }
done
echo "signed $(find "$dist/bin" "$dist/lib" -type f | wc -l | tr -d ' ') files with identity '$identity'${entitlements:+ and $(basename "$entitlements")}"
