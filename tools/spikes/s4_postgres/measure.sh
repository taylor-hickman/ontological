#!/usr/bin/env bash
# Print the size of a trim.sh tree as a Markdown table, against @embedded-postgres/darwin-arm64.
# Sizes are file bytes (what npm calls "unpacked size"), not disk blocks.
#
#   measure.sh <dist> [<archive>]
set -euo pipefail

dist="$(cd "$1" && pwd)"
archive="${2:-}"
embedded_mb=337 # @embedded-postgres/darwin-arm64, unpacked (ARCHITECTURE.md §10)

bytes() { find "$@" -type f -print0 | xargs -0 stat -f%z | awk '{s += $1} END {print s + 0}'; }
mb() { awk -v b="$1" 'BEGIN {printf "%.1f", b / 1000000}'; }

total="$(bytes "$dist")"
echo "| Part | Files | Size (MB) |"
echo "|---|---:|---:|"
for d in bin lib share; do
  echo "| \`$d/\` | $(find "$dist/$d" -type f | wc -l | tr -d ' ') | $(mb "$(bytes "$dist/$d")") |"
done
echo "| **Total, unpacked** | $(find "$dist" -type f | wc -l | tr -d ' ') | **$(mb "$total")** |"
if [[ -n "$archive" ]]; then
  echo "| $(basename "$archive") | 1 | $(mb "$(stat -f%z "$archive")") |"
fi
echo "| \`@embedded-postgres/darwin-arm64\`, unpacked | | $embedded_mb |"
echo
awk -v t="$total" -v e="$embedded_mb" 'BEGIN {printf "Trimmed build is %.1f%% of the embedded-postgres package.\n", 100 * t / (e * 1000000)}'
