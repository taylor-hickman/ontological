#!/usr/bin/env bash
# Why entitlements.plist exists: sign a copy of a trim.sh tree with the hardened runtime and no
# entitlements, and check that library validation then refuses to load our own libraries.
# Exits 0 when they are refused, as expected for an ad-hoc signature. With a Developer ID every
# file shares one Team ID, so if this starts failing under CODESIGN_IDENTITY, the entitlement can
# probably be dropped (M7).
#
#   control.sh <dist>
#
# Library validation is only enforced with System Integrity Protection on. GitHub's macOS
# runners have it off, so there this prints why and exits 0 without checking anything.
set -euo pipefail

sip="$(csrutil status 2>&1 || true)"
echo "$sip"
if [[ "$sip" != *"status: enabled."* ]]; then
  echo "skip - library validation is not enforced on this machine, so this check needs a Mac with SIP on"
  exit 0
fi

here="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
ditto "$1" "$tmp/postgres"
"$here/sign.sh" "$tmp/postgres" >/dev/null

# initdb links libpq.5.dylib, so dyld refuses it at launch.
if out="$("$tmp/postgres/bin/initdb" --version 2>&1)"; then
  echo "initdb ran without disable-library-validation: $out"
  exit 1
fi
grep -q 'different Team IDs' <<<"$out" || { echo "initdb failed for another reason: $out"; exit 1; }
echo "ok - without entitlements, dyld refuses libpq.5.dylib in initdb (different Team IDs)"

# postgres links no libpq and starts, but dlopen of its modules is refused. Initialise and
# start with the entitled signature, then re-sign postgres alone without entitlements.
"$here/sign.sh" "$tmp/postgres" "$here/entitlements.plist" >/dev/null
bin="$tmp/postgres/bin"
mkdir "$tmp/sock"
"$bin/initdb" -D "$tmp/data" -E UTF8 --locale=C -A trust -U ontological >/dev/null
codesign --force --sign "${CODESIGN_IDENTITY:--}" --options runtime "$bin/postgres" 2>/dev/null
"$bin/pg_ctl" -D "$tmp/data" -l "$tmp/server.log" -w -o "-k $tmp/sock -p 54328 -c listen_addresses=''" start >/dev/null
trap '"$bin/pg_ctl" -D "$tmp/data" -m immediate stop >/dev/null 2>&1; rm -rf "$tmp"' EXIT
if out="$("$bin/psql" -X -h "$tmp/sock" -p 54328 -U ontological -d postgres -c 'CREATE EXTENSION vector' 2>&1)"; then
  echo "postgres loaded vector.dylib without disable-library-validation"
  exit 1
fi
grep -q 'different Team IDs' <<<"$out" || { echo "CREATE EXTENSION failed for another reason: $out"; exit 1; }
echo "ok - without entitlements, postgres refuses to dlopen vector.dylib (different Team IDs)"
