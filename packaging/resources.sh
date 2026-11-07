#!/usr/bin/env bash
# Assemble what the app bundles beside the engine (ARCHITECTURE.md §10) into <out>, which
# electron-builder copies into Ontological.app/Contents/Resources (apps/desktop/electron-builder.yml):
#
#   postgres/            S4's trimmed PostgreSQL 17 + pg_trgm + pgvector tree, signed with the
#                        hardened runtime and entitlements.plist on the executables
#   model/               the default embedding model's pinned files (engine/ontological/embed/model.py)
#   duckdb-extensions/   the source scanners for the locked DuckDB, laid out as extension_directory
#
#   packaging/resources.sh [<out>] [<postgres.tar.xz>]
#
# <out> defaults to apps/desktop/resources and is replaced. With a tarball (postgres.yml's
# artifact), Postgres is unpacked from it as built; without one, it is built, trimmed and signed
# with tools/spikes/s4_postgres (under a minute on an M-series Mac, Xcode's command line tools
# needed). The model and extensions are downloaded and checked against their pins; set
# ONTOLOGICAL_MODEL_DIR or ONTOLOGICAL_DUCKDB_EXTENSIONS to copy them from local copies first.
# packaging/check.sh runs on the result last.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:-$repo/apps/desktop/resources}"
tarball="${2:-}"
[[ -z "$tarball" ]] || tarball="$(cd "$(dirname "$tarball")" && pwd)/$(basename "$tarball")"
s4="$repo/tools/spikes/s4_postgres"
engine_py() { uv run --quiet --project "$repo/engine" --locked python "$@"; }

rm -rf "$out"
mkdir -p "$out"
out="$(cd "$out" && pwd)"

# postgres/
if [[ -n "$tarball" ]]; then
  mkdir "$out/postgres"
  tar -C "$out/postgres" -xJf "$tarball"
else
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  "$s4/build.sh" "$work" >"$work/build.log" 2>&1 || { tail -40 "$work/build.log" >&2; exit 1; }
  "$s4/trim.sh" "$work/postgres" "$out/postgres"
  "$s4/sign.sh" "$out/postgres" "$s4/entitlements.plist"
fi

# model/: only the pinned files, which fetch() skips when a copy already verifies.
if [[ -n "${ONTOLOGICAL_MODEL_DIR:-}" ]]; then
  while read -r name; do
    [[ -f "$ONTOLOGICAL_MODEL_DIR/$name" ]] || continue
    mkdir -p "$(dirname "$out/model/$name")"
    cp "$ONTOLOGICAL_MODEL_DIR/$name" "$out/model/$name"
  done < <(engine_py -c 'from ontological.embed.model import FILES; print(*FILES, sep="\n")')
fi
engine_py -m ontological.embed.model "$out/model"

# duckdb-extensions/: <version>/<platform>/<name>.duckdb_extension, which install() skips when
# already there.
if [[ -n "${ONTOLOGICAL_DUCKDB_EXTENSIONS:-}" ]]; then
  sub="$(engine_py -c 'import duckdb; print(f"v{duckdb.__version__}/" + duckdb.connect().execute("PRAGMA platform").fetchone()[0])')"
  if [[ -d "$ONTOLOGICAL_DUCKDB_EXTENSIONS/$sub" ]]; then
    mkdir -p "$out/duckdb-extensions/$sub"
    cp "$ONTOLOGICAL_DUCKDB_EXTENSIONS/$sub/"*_scanner.duckdb_extension* "$out/duckdb-extensions/$sub/"
  fi
fi
engine_py -m ontological.sources.extensions "$out/duckdb-extensions"

"$repo/packaging/check.sh" "$out"
