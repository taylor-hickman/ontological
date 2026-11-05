#!/usr/bin/env bash
# Check that a signed trim.sh tree works the way the engine will use it (ARCHITECTURE.md §5.3):
# copied to a path with spaces, initdb with UTF-8 and locale C, start on a Unix socket only,
# load vector and pg_trgm, fork client backends and a parallel worker, dump and restore.
#
#   smoke.sh <dist>
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=versions.env
source "$here/versions.env"
src="$(cd "$1" && pwd)"
tmp="$(mktemp -d)"
port=54329
user=ontological

# Run from a copy under a path with spaces, like an app bundle and ~/Library/Application Support.
app="$tmp/App Bundle/postgres"
data="$tmp/App Data/pgdata"
sock="$tmp/sock" # no spaces, and short: Unix socket paths max out at 103 bytes
mkdir -p "$(dirname "$app")" "$(dirname "$data")" "$sock"
ditto "$src" "$app"
bin="$app/bin"
log="$tmp/server.log"

stop() {
  "$bin/pg_ctl" -D "$data" -m immediate stop >/dev/null 2>&1 || true
}
fail() {
  echo "FAIL: $*" >&2
  echo "--- server log ---" >&2
  cat "$log" >&2 2>/dev/null || true
  stop
  exit 1
}
trap 'fail "command failed at line $LINENO"' ERR

sql() { "$bin/psql" -X -q -t -A -v ON_ERROR_STOP=1 -h "$sock" -p "$port" -U "$user" "$@"; }
expect() { # description expected actual
  [[ "$3" == "$2" ]] || fail "$1: expected '$2', got '$3'"
  echo "ok - $1"
}

"$bin/initdb" -D "$data" -E UTF8 --locale=C -A trust -U "$user" >"$tmp/initdb.log" 2>&1 ||
  { cat "$tmp/initdb.log" >&2; fail "initdb"; }
echo "ok - initdb (loads plpgsql.dylib and dict_snowball.dylib)"

"$bin/pg_ctl" -D "$data" -l "$log" -w -t 30 \
  -o "-k $sock -p $port -c listen_addresses=''" start >/dev/null
"$bin/pg_isready" -q -h "$sock" -p "$port" || fail "pg_isready"
echo "ok - server started on a Unix socket"

expect "server version" 17 "$(sql -d postgres -c 'SHOW server_version_num' | cut -c1-2)"
expect "encoding and collation" "UTF8 C" \
  "$(sql -d postgres -c "SELECT pg_encoding_to_char(encoding) || ' ' || datcollate FROM pg_database WHERE datname = 'postgres'")"

# Background processes are forked by the postmaster at start.
procs="$(sql -d postgres -c "SELECT string_agg(DISTINCT backend_type, ',' ORDER BY backend_type) FROM pg_stat_activity")"
for p in checkpointer "autovacuum launcher" walwriter; do
  [[ ",$procs," == *",$p,"* ]] || fail "no $p process (have: $procs)"
done
echo "ok - postmaster forked its background processes"

sql -d postgres >/dev/null <<'SQL'
CREATE EXTENSION vector;
CREATE EXTENSION pg_trgm;
CREATE TABLE items (id int PRIMARY KEY, name text, embedding vector(3));
INSERT INTO items
  SELECT i, 'table_' || i, ARRAY[sin(i), cos(i), i % 7]::vector
  FROM generate_series(1, 2000) i;
CREATE INDEX items_embedding ON items USING hnsw (embedding vector_cosine_ops);
CREATE INDEX items_name ON items USING gin (name gin_trgm_ops);
ANALYZE items;
SQL
expect "vector.dylib loaded" "$PGVECTOR_VERSION" \
  "$(sql -d postgres -c "SELECT extversion FROM pg_extension WHERE extname = 'vector'")"
expect "vector distance" 5.196 "$(sql -d postgres -c "SELECT round(('[1,2,3]'::vector <-> '[4,5,6]')::numeric, 3)")"
plan="$(sql -d postgres -c "SET enable_seqscan = off; EXPLAIN SELECT id FROM items ORDER BY embedding <=> '[0,1,3]' LIMIT 5")"
[[ "$plan" == *items_embedding* ]] || fail "HNSW index not used: $plan"
expect "HNSW nearest neighbours" 5 \
  "$(sql -d postgres -c "SET enable_seqscan = off; SELECT count(*) FROM (SELECT id FROM items ORDER BY embedding <=> '[0,1,3]' LIMIT 5) t")"
expect "pg_trgm similarity" t "$(sql -d postgres -c "SELECT similarity('customer_id', 'custmer_id') > 0.5")"

# A parallel worker is a background worker the postmaster forks on demand.
plan="$(sql -d postgres -c "SET debug_parallel_query = on; EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF) SELECT count(*) FROM items")"
[[ "$plan" == *"Workers Launched: 1"* ]] || fail "no parallel worker launched: $plan"
echo "ok - parallel worker forked"

# Each connection is a backend forked by the postmaster.
pids=()
for _ in 1 2 3 4 5 6 7 8; do
  sql -d postgres -c "SELECT pg_sleep(3)" >/dev/null &
  pids+=($!)
done
n=0
for _ in $(seq 1 20); do
  n="$(sql -d postgres -c "SELECT count(*) FROM pg_stat_activity WHERE query = 'SELECT pg_sleep(3)'")"
  [[ "$n" == 8 ]] && break
  sleep 0.1
done
expect "8 concurrent client backends" 8 "$n"
for p in "${pids[@]}"; do wait "$p"; done

# pg_dump custom format (zlib) and pg_restore, as the pre-migration and quit dumps do.
"$bin/pg_dump" -Fc -h "$sock" -p "$port" -U "$user" -f "$tmp/store.dump" postgres
sql -d postgres -c "CREATE DATABASE restored" >/dev/null
"$bin/pg_restore" -h "$sock" -p "$port" -U "$user" -d restored "$tmp/store.dump"
expect "restored rows" 2000 "$(sql -d restored -c "SELECT count(*) FROM items")"
expect "restored vector query" \
  "$(sql -d postgres -c "SELECT id FROM items ORDER BY embedding <-> '[0,1,3]', id LIMIT 1")" \
  "$(sql -d restored -c "SELECT id FROM items ORDER BY embedding <-> '[0,1,3]', id LIMIT 1")"

"$bin/pg_ctl" -D "$data" -m fast -w stop >/dev/null
if grep -Eiq 'code signature|library validation|not valid for use|team id' "$log"; then
  fail "code-signing errors in the server log"
fi
echo "ok - stopped cleanly; no code-signing errors in the server log"
rm -rf "$tmp"
