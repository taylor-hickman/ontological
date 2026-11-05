#!/usr/bin/env bash
# Build PostgreSQL 17 from source with a minimal configure, plus pg_trgm and pgvector,
# into <work>/stage. trim.sh then copies the shipped subset out of it.
#
#   build.sh <work-dir>
#
# VERSIONS_ENV=<file> builds the versions pinned there instead of versions.env (spike S5 builds
# PostgreSQL 16 this way).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=versions.env
source "${VERSIONS_ENV:-$here/versions.env}"
export MACOSX_DEPLOYMENT_TARGET

work="$(mkdir -p "$1" && cd "$1" && pwd)"
# The prefix contains "postgres", so PG installs a flat layout (lib/, share/) with no
# postgresql/ subdirectories, whatever the parent path is. trim.sh relies on that.
stage="$work/postgres"
src="$work/src"
jobs="$(sysctl -n hw.ncpu)"

fetch() { # url sha256 dest
  if [[ ! -f "$3" ]]; then curl -sfL --retry 3 -o "$3.part" "$1" && mv "$3.part" "$3"; fi
  echo "$2  $3" | shasum -a 256 -c -
}

mkdir -p "$src"
fetch "https://ftp.postgresql.org/pub/source/v$PG_VERSION/postgresql-$PG_VERSION.tar.bz2" \
  "$PG_SHA256" "$work/postgresql-$PG_VERSION.tar.bz2"
fetch "https://github.com/pgvector/pgvector/archive/refs/tags/v$PGVECTOR_VERSION.tar.gz" \
  "$PGVECTOR_SHA256" "$work/pgvector-$PGVECTOR_VERSION.tar.gz"

rm -rf "$src"/* "$stage"
tar -xjf "$work/postgresql-$PG_VERSION.tar.bz2" -C "$src"
tar -xzf "$work/pgvector-$PGVECTOR_VERSION.tar.gz" -C "$src"

# Keep Homebrew out of the build so nothing links against /opt/homebrew.
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
unset PKG_CONFIG_PATH CPPFLAGS LDFLAGS CFLAGS

# Minimal configure. ICU is on by default since PG 16; locale C doesn't need it.
# readline comes from the system libedit, zlib from the system libz (pg_dump -Fc compresses).
# No OpenSSL (Unix socket only), no NLS, no LLVM JIT, no lz4/zstd/libxml (all off by default).
cd "$src/postgresql-$PG_VERSION"
./configure --prefix="$stage" --without-icu --with-libedit-preferred --disable-nls >"$work/configure.log"
make -j"$jobs" -s >"$work/make.log"
make -s install >>"$work/make.log"
# pg_trgm: the hybrid search's name-similarity rank (ARCHITECTURE.md §7).
make -s -C contrib/pg_trgm install >>"$work/make.log"

# pgvector. OPTFLAGS="" drops -march=native so the build runs on any arm64 Mac.
cd "$src/pgvector-$PGVECTOR_VERSION"
make -j"$jobs" -s PG_CONFIG="$stage/bin/pg_config" OPTFLAGS="" >>"$work/make.log"
make -s PG_CONFIG="$stage/bin/pg_config" install >>"$work/make.log"

echo "built PostgreSQL $PG_VERSION + pgvector $PGVECTOR_VERSION into $stage"
