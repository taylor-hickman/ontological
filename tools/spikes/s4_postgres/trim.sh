#!/usr/bin/env bash
# Copy the shipped subset of a build.sh install (ARCHITECTURE.md §10) into <dist> and make it
# relocatable: libpq is linked through @rpath, so the tree runs from wherever the app puts it.
# The result is unsigned; run sign.sh next (arm64 macOS won't run unsigned code).
#
#   trim.sh <stage> <dist>
set -euo pipefail

stage="$(cd "$1" && pwd)"
dist="$2"
rm -rf "$dist"
mkdir -p "$dist/bin" "$dist/lib"
dist="$(cd "$dist" && pwd)"

# bin/: what the engine runs (§5.3) plus psql for debugging.
for b in postgres initdb pg_ctl pg_isready pg_dump pg_restore psql; do
  cp "$stage/bin/$b" "$dist/bin/"
done

# lib/: libpq and the server's loadable modules (plpgsql, snowball, encoding conversions,
# walreceiver/pgoutput, pg_trgm, vector). Dropped: static libs, pgxs, pkgconfig, the ecpg
# libraries and the unversioned libpq.dylib dev symlink.
cp "$stage/lib/libpq.5.dylib" "$dist/lib/"
for f in "$stage"/lib/*.dylib; do
  case "$(basename "$f")" in
    libpq.dylib | libpq.5.dylib | libecpg* | libpgtypes*) ;;
    *) cp "$f" "$dist/lib/" ;;
  esac
done

# share/: everything build.sh installs is needed at runtime (initdb's catalog scripts,
# timezone data, text search data, extension scripts incl. upgrade paths). No docs or man
# pages are built, and include/ is not copied.
cp -R "$stage/share" "$dist/share"

# Relocate: rewrite every reference into <stage>/lib to @rpath and give each file an rpath
# to lib/.
macho_files() { find "$dist/bin" "$dist/lib" -type f -perm -u+x; }
libpq_old="$(otool -D "$dist/lib/libpq.5.dylib" | tail -1)"
install_name_tool -id @rpath/libpq.5.dylib "$dist/lib/libpq.5.dylib" 2>/dev/null
while read -r f; do
  if otool -L "$f" | grep -qF "$libpq_old"; then
    case "$f" in
      "$dist"/bin/*) rpath=@loader_path/../lib ;;
      *) rpath=@loader_path ;;
    esac
    install_name_tool -change "$libpq_old" @rpath/libpq.5.dylib -add_rpath "$rpath" "$f" 2>/dev/null
  fi
done < <(macho_files)

# Every dependency must now be inside the tree or part of macOS.
bad=0
while read -r f; do
  while read -r dep; do
    case "$dep" in
      @rpath/* | /usr/lib/* | /System/*) ;;
      *) echo "non-relocatable dependency in ${f#"$dist"/}: $dep" >&2; bad=1 ;;
    esac
  done < <(otool -L "$f" | tail -n +2 | awk '{print $1}' | grep -vxF "$(otool -D "$f" | tail -n +2)" || true)
done < <(macho_files)
if grep -rqF "$stage" "$dist/share"; then
  echo "share/ mentions the build prefix $stage" >&2; bad=1
fi
exit "$bad"
