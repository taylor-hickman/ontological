# S4: bundled PostgreSQL 17 + pgvector

Builds the Postgres the app ships (ARCHITECTURE.md §10) and checks it still works once it is trimmed, moved and signed with the hardened runtime. `.github/workflows/postgres.yml` runs these steps on macOS arm64 and uploads the result as a `.tar.xz` artifact. M7.2 takes its binaries from that job.

```sh
./build.sh  <work>                       # PostgreSQL + pg_trgm + pgvector into <work>/postgres
./trim.sh   <work>/postgres <work>/dist  # shipped file list, libpq linked through @rpath
./sign.sh   <work>/dist entitlements.plist
./control.sh <work>/dist                 # without the entitlement, our libraries are refused
./smoke.sh  <work>/dist                  # initdb, start, forks, vector, pg_trgm, dump/restore
./measure.sh <work>/dist [archive]       # size table vs @embedded-postgres/darwin-arm64
```

Versions and checksums are pinned in `versions.env`. A local build takes under a minute on an M-series Mac.

## What gets built

- `configure --without-icu --with-libedit-preferred --disable-nls`. ICU is the only default-on library dropped (locale `C` doesn't need it). OpenSSL, LLVM, lz4, zstd and libxml are off by default and stay off. readline comes from macOS's libedit, and zlib from macOS's libz (pg_dump's custom format compresses with it). `build.sh` runs with Homebrew off `PATH`, and `trim.sh` fails if any file depends on a library outside the tree or macOS.
- Contrib: only `pg_trgm`. Upstream's Alembic chain creates `pg_trgm` and `vector` and nothing else, and `gen_random_uuid()` is in core.
- pgvector with `OPTFLAGS=""`, so the build has no `-march=native` and runs on any arm64 Mac.
- `MACOSX_DEPLOYMENT_TARGET=13.0`.

## What ships

`bin/`: `postgres`, `initdb`, `pg_ctl`, `pg_isready`, `pg_dump`, `pg_restore`, `psql`. `lib/`: `libpq.5.dylib` and the server's loadable modules. `share/`: all of it, since the install has no docs or man pages. Headers, static libraries, pgxs, pkgconfig and the ecpg libraries are dropped.

The tree is relocatable: Postgres finds `share/` and `lib/` relative to its own binary, and `trim.sh` relinks libpq as `@rpath/libpq.5.dylib`. `smoke.sh` runs it from a copy under a path with spaces.

## Signing

With the hardened runtime, library validation only maps a library signed by Apple or by the process's own Team ID. An ad-hoc signature has no Team ID, so without an entitlement dyld refuses `libpq.5.dylib` in `initdb`, `psql`, `pg_dump` and `pg_restore`, and `postgres` can't `dlopen` `plpgsql`, `dict_snowball` or `vector`. `initdb` therefore fails before it finishes (`control.sh` shows both failures). `entitlements.plist` sets `com.apple.security.cs.disable-library-validation` on the executables, and with it everything in `smoke.sh` passes.

Library validation is only enforced with System Integrity Protection on, and GitHub's macOS runners have it off. So in CI the signed tree passes `smoke.sh` with or without the entitlement, and `control.sh` skips. The entitlement finding comes from a Mac with SIP on: run `control.sh` and `smoke.sh` locally after changing how the tree is built or signed.

Forking needs nothing: on macOS `postgres` forks its backends and background workers without `exec`, and there is no JIT to need `allow-jit`.

With a Developer ID every file carries the same Team ID, so library validation should pass without the entitlement. M7 can check that by running `control.sh` with `CODESIGN_IDENTITY` set: if it starts failing (the libraries load), drop the entitlement.
