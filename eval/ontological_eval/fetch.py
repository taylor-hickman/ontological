"""Download the eval data named in sources.json (BUILD_PLAN.md, Eval set).

Every file is pinned: plain URLs at a fixed commit or revision, and a sha256 for each file, which
a download has to match before it replaces anything. A file already on disk with the right hash
isn't downloaded again.

BIRD publishes its SQLite databases only inside minidev.zip (800 MB, with every Mini-Dev
database plus MySQL and Postgres dumps). The four databases the eval uses are about 46 MB of it,
so they come out of the zip with HTTP range requests: the zip's central directory, then each
member's compressed bytes. Nothing else in the zip is fetched, `database_description/*.csv`
included. ontology-sql-eval's `custom_analyses.json` isn't listed either: neither is ingested.
"""

from __future__ import annotations

import hashlib
import io
import json
import struct
import sys
import urllib.request
import zipfile
import zlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from http.client import HTTPResponse
from pathlib import Path
from typing import NotRequired, TypedDict, cast

SOURCES = Path(__file__).with_name("sources.json")
CHUNK = 1 << 20
USER_AGENT = "ontological-eval"
TIMEOUT_S = 60


class FetchError(RuntimeError):
    pass


class Group(TypedDict):
    """A group in sources.json: files under `base`, or members of the zip at `zip`, each with
    its sha256, downloaded into <home>/<dest>."""

    about: NotRequired[str]
    base: NotRequired[str]
    zip: NotRequired[str]
    dest: str
    files: dict[str, str]


Sources = dict[str, Group]


@dataclass(frozen=True)
class Item:
    """One file to fetch: from `url`, or from member `member` of the zip at `url`."""

    url: str
    dest: Path
    sha256: str
    member: str | None = None


def load_sources(path: Path = SOURCES) -> Sources:
    return cast(Sources, json.loads(path.read_text()))


def items(home: Path, sources: Sources, groups: Iterable[str]) -> list[Item]:
    """The files of `groups`. A plain group's file lands at <home>/<dest>/<its path>; a zip
    member at <home>/<dest>/<its file name>."""
    out: list[Item] = []
    for name in groups:
        if name not in sources:
            raise FetchError(f"no source group {name!r}; sources.json has {sorted(sources)}")
        g = sources[name]
        dest = home / g["dest"]
        if "zip" in g:
            out += [Item(g["zip"], dest / Path(m).name, h, m) for m, h in g["files"].items()]
        elif "base" in g:
            out += [Item(g["base"] + rel, dest / rel, h) for rel, h in g["files"].items()]
        else:
            raise FetchError(f"source group {name!r} has neither `base` nor `zip`")
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(CHUNK):
            h.update(b)
    return h.hexdigest()


def is_current(item: Item) -> bool:
    return item.dest.is_file() and sha256_file(item.dest) == item.sha256


def fetch(
    home: Path,
    groups: Iterable[str],
    *,
    sources: Sources | None = None,
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
) -> list[Path]:
    """Download whatever of `groups` is missing or wrong; the paths it wrote."""
    todo = [i for i in items(home, sources or load_sources(), groups) if not is_current(i)]
    written: list[Path] = []
    for item in (i for i in todo if i.member is None):
        log(f"fetch {item.dest.relative_to(home)}")
        with _open(item.url) as resp:
            _write(item, _chunks(resp))
        written.append(item.dest)
    for url in dict.fromkeys(i.url for i in todo if i.member is not None):
        members = [i for i in todo if i.url == url and i.member is not None]
        log(f"fetch {len(members)} member(s) of {url}")
        written += _fetch_members(url, members, home, log)
    return written


# --- plain files ---------------------------------------------------------------------------------


def _open(url: str, first: int | None = None, last: int | None = None) -> HTTPResponse:
    """GET `url`, or bytes first..last of it, which must come back as a 206."""
    headers = {"User-Agent": USER_AGENT}
    if first is not None:
        headers["Range"] = f"bytes={first}-{last}"
    resp = cast(
        HTTPResponse,
        urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT_S),
    )
    if first is not None and resp.status != 206:
        resp.close()
        raise FetchError(f"{url} answered a range request with HTTP {resp.status}, not 206")
    return resp


def _chunks(f: HTTPResponse) -> Iterator[bytes]:
    while b := f.read(CHUNK):
        yield b


def _write(item: Item, chunks: Iterable[bytes]) -> None:
    """Write to <dest>.part, and rename it over <dest> only if its sha256 matches."""
    item.dest.parent.mkdir(parents=True, exist_ok=True)
    part = item.dest.with_name(item.dest.name + ".part")
    h = hashlib.sha256()
    try:
        with open(part, "wb") as out:
            for b in chunks:
                h.update(b)
                out.write(b)
        if h.hexdigest() != item.sha256:
            raise FetchError(
                f"{item.url}{f' ({item.member})' if item.member else ''} has sha256 "
                f"{h.hexdigest()}, but sources.json pins {item.sha256}"
            )
        part.replace(item.dest)
    finally:
        part.unlink(missing_ok=True)


# --- members of a remote zip ---------------------------------------------------------------------


def _fetch_members(
    url: str, members: list[Item], home: Path, log: Callable[[str], None]
) -> list[Path]:
    remote = _RangeFile(url)
    with zipfile.ZipFile(io.BufferedReader(remote, 1 << 16)) as z:
        infos = {i.filename: i for i in z.infolist()}
    written: list[Path] = []
    for item in members:
        assert item.member is not None
        info = infos.get(item.member)
        if info is None:
            raise FetchError(f"{url} has no member {item.member}")
        log(f"fetch {item.dest.relative_to(home)} ({info.compress_size / 2**20:.1f} MB)")
        header = remote.get(info.header_offset, 30)
        if header[:4] != b"PK\x03\x04":
            raise FetchError(f"{url}: no local file header for {item.member}")
        name_len, extra_len = struct.unpack("<HH", header[26:30])
        start = info.header_offset + 30 + name_len + extra_len
        with _open(url, start, start + info.compress_size - 1) as resp:
            _write(item, _inflate(info, _chunks(resp), url))
        written.append(item.dest)
    return written


def _inflate(info: zipfile.ZipInfo, chunks: Iterable[bytes], url: str) -> Iterator[bytes]:
    """A member's bytes from its compressed bytes, checking its CRC-32 and size."""
    if info.compress_type == zipfile.ZIP_DEFLATED:
        d = zlib.decompressobj(-zlib.MAX_WBITS)
        decode: Callable[[bytes], bytes] = d.decompress
        flush: Callable[[], bytes] = d.flush
    elif info.compress_type == zipfile.ZIP_STORED:
        decode, flush = (lambda b: b), (lambda: b"")
    else:
        raise FetchError(f"{url}: {info.filename} uses compression method {info.compress_type}")
    crc, size = 0, 0
    for b in _stream(chunks, decode, flush):
        crc, size = zlib.crc32(b, crc), size + len(b)
        yield b
    if crc != info.CRC or size != info.file_size:
        raise FetchError(f"{url}: {info.filename} failed its CRC-32 or size check")


def _stream(
    chunks: Iterable[bytes], decode: Callable[[bytes], bytes], flush: Callable[[], bytes]
) -> Iterator[bytes]:
    for b in chunks:
        yield decode(b)
    yield flush()


class _RangeFile(io.RawIOBase):
    """A remote file read with HTTP range requests, for zipfile to find the central
    directory in."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.pos = 0
        with _open(url, 0, 0) as resp:
            self.size = int(resp.headers["Content-Range"].rsplit("/", 1)[1])

    def get(self, offset: int, n: int) -> bytes:
        with _open(self.url, offset, offset + n - 1) as resp:
            return resp.read()

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence]
        self.pos = base + offset
        return self.pos

    def readinto(self, buffer: bytearray | memoryview) -> int:  # type: ignore[override]
        n = min(len(buffer), self.size - self.pos)
        if n <= 0:
            return 0
        data = self.get(self.pos, n)
        buffer[: len(data)] = data
        self.pos += len(data)
        return len(data)
