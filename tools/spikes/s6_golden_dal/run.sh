#!/usr/bin/env bash
# Spike S6: run upstream's golden fixture seed and DAL tests against a bare Postgres built by S4,
# outside Docker (ARCHITECTURE.md §5.4, docs/BUILD_PLAN.md S6).
#
#   run.sh <work> <s4-dist>
#
# <work> gets the upstream checkout, the test cluster and the reports. <s4-dist> is a signed
# tree from tools/spikes/s4_postgres (build.sh, trim.sh, sign.sh). The steps are upstream's own
# CI job (.github/workflows/ci-python.yml, "Pytest (with Postgres fixtures)") with the
# pgvector/pgvector:pg17 service container swapped for an initdb'd cluster on 127.0.0.1.
#
# Exits non-zero if any step fails, or if any DAL test fails or skips.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=versions.env
source "$here/versions.env"
mkdir -p "$1"
work="$(cd "$1" && pwd)"
dist="$(cd "$2" && pwd)"
bin="$dist/bin"
upstream="$work/upstream"
data="$work/pgdata"
sock="$(mktemp -d /tmp/s6.XXXXXX)" # Unix socket paths max out at 103 bytes
reports="$work/reports"
log="$work/server.log"
port="${S6_PORT:-55432}"

export POSTGRES_HOST=127.0.0.1
export POSTGRES_PORT="$port"
export POSTGRES_USER=auto_ontology
export POSTGRES_PASSWORD=auto_ontology
export POSTGRES_DATABASE=auto_ontology
export PGPASSWORD="$POSTGRES_PASSWORD"
# Upstream's tests load a .env through python-dotenv (auto_ontology/tests/conftest.py); the
# checkout has none, so only what is exported here reaches them.
unset DATABASE_URL CONNECTION_STRINGS

step() { echo "== $* =="; }

stop() {
  [[ -f "$data/postmaster.pid" ]] && "$bin/pg_ctl" -D "$data" -m fast stop >/dev/null 2>&1
  rm -rf "$sock"
}
trap stop EXIT

step "upstream at $UPSTREAM_COMMIT"
if [[ ! -d "$upstream/.git" ]]; then
  git init --quiet "$upstream"
  git -C "$upstream" remote add origin "$UPSTREAM_REPO"
fi
if [[ "$(git -C "$upstream" rev-parse -q --verify HEAD || true)" != "$UPSTREAM_COMMIT" ]]; then
  git -C "$upstream" fetch --quiet --depth 1 origin "$UPSTREAM_COMMIT"
  git -C "$upstream" checkout --quiet --force --detach FETCH_HEAD
fi
git -C "$upstream" clean --quiet -fdx -e .venv
(cd "$upstream" && uv sync --locked --quiet)

step "test cluster on $bin"
rm -rf "$data" "$reports" "$log"
mkdir -p "$reports"
pwfile="$work/pwfile"
printf '%s\n' "$POSTGRES_PASSWORD" >"$pwfile"
# The same initdb the engine will run (UTF-8, locale C), with password auth because upstream
# connects over TCP with POSTGRES_USER/POSTGRES_PASSWORD, as it does to the Docker image.
"$bin/initdb" -D "$data" -E UTF8 --locale=C -A scram-sha-256 -U "$POSTGRES_USER" \
  --pwfile="$pwfile" >"$work/initdb.log"
"$bin/pg_ctl" -D "$data" -l "$log" -w -t 30 \
  -o "-k $sock -p $port -c listen_addresses=127.0.0.1" start >/dev/null
"$bin/psql" -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -p "$port" -U "$POSTGRES_USER" -d postgres \
  -c "CREATE DATABASE $POSTGRES_DATABASE"

cd "$upstream"
py() { uv run --no-sync "$@"; }
# s6_loaded records which upstream modules the seed and the tests load, for imports.py.
export S6_LOADED="$reports/loaded.txt"

step "fixture databases (pagila in Postgres, chinook as SQLite)"
py python -m dev_tools.fixtures.seed_fixtures

step "alembic upgrade head"
py alembic upgrade head

step "seed_graph_fixture --reset"
CONNECTION_STRINGS="postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@127.0.0.1:$port/pagila,sqlite:///$upstream/dev_tools/fixtures/sql/chinook.sqlite" \
  PYTHONPATH="$here" py python -m s6_loaded dev_tools.fixtures.seed_graph_fixture --reset

step "alembic check (migrations describe the models)"
py alembic check

step "pytest auto_ontology/dal/tests"
set +e
PYTHONPATH="$here" py pytest auto_ontology/dal/tests -q -rs -p no:cacheprovider -p s6_loaded \
  --junitxml="$reports/dal.xml" >"$reports/dal.out" 2>&1
set -e
tail -n 30 "$reports/dal.out"

step "the same tests over the Unix socket only, as the engine connects (§5.3)"
# Expected to skip until infra/postgres.py is edited: its URL puts POSTGRES_HOST before
# ":port", which can't name a socket directory, so psycopg falls back to /tmp/.s.PGSQL.5432
# and the tests' "schema unavailable" guards skip. Reported, not counted in the exit status.
"$bin/pg_ctl" -D "$data" -m fast -w stop >/dev/null
"$bin/pg_ctl" -D "$data" -l "$log" -w -t 30 -o "-k $sock -p $port -c listen_addresses=''" \
  start >/dev/null
POSTGRES_HOST="$sock" py pytest auto_ontology/dal/tests -q -p no:cacheprovider \
  --junitxml="$reports/dal-socket.xml" >"$reports/dal-socket.out" 2>&1 || true
tail -n 1 "$reports/dal-socket.out"
py python -I "$here/report.py" "$reports/dal-socket.xml" >"$reports/dal-socket.md" || true

step "imports from outside ARCHITECTURE.md §11's vendored set"
py python -I "$here/imports.py" . "$reports/loaded.txt" >"$reports/imports.md"
echo "written to $reports/imports.md"

step "pass/fail list"
py python -I "$here/report.py" "$reports/dal.xml" | tee "$reports/dal.md"
