"""Byte-exact transcript scanning for incremental sync.

A transcript is append-only while its agent runs. Incremental sync resumes
parsing at a checkpoint: the byte offset just past the last line terminator
it consumed. Resuming is only sound when the bytes before that offset are the
ones parsed last time, so one sequential read computes the whole-file SHA-256
(the session's ``file_hash``), the SHA-256 of the old checkpoint prefix, and
the SHA-256 of the new checkpoint prefix, without a second pass.
"""

from __future__ import annotations

import errno
import hashlib
import io
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_READ_CHUNK = 1024 * 1024
# Universal-newline terminators, matching text-mode iteration in parsers.py.
_TERMINATORS = (b"\n", b"\r")


@dataclass(frozen=True)
class TranscriptScan:
    """One consistent read of ``size`` bytes of a transcript."""

    size: int
    sha256: str
    dev: int
    ino: int
    mtime: float
    # SHA-256 of bytes [0, offset) for each requested offset <= size.
    prefix_sha256s: dict[int, str]
    # Offset just past the last line terminator in [0, size), and the SHA-256
    # of the bytes before it. Bytes after it form an unterminated tail.
    line_end: int
    line_end_sha256: str

    def prefix_matches(self, offset: int | None, expected_sha256: str | None) -> bool:
        """Whether bytes [0, offset) are exactly the ones hashed earlier."""
        return bool(
            offset is not None
            and expected_sha256
            and self.prefix_sha256s.get(offset) == expected_sha256
        )


def _last_terminator_end(chunk: bytes) -> int:
    """Index just past the last universal-newline terminator, or -1."""
    return max(chunk.rfind(terminator) for terminator in _TERMINATORS) + 1 or -1


def scan_transcript(
    path: Path, *, prefix_offsets: Iterable[int | None] = ()
) -> TranscriptScan:
    """Hash exactly the bytes present at open time.

    The size is taken from ``fstat`` on the open descriptor, and the read
    stops there even if the writer appends meanwhile, so ``sha256`` always
    describes exactly ``size`` bytes. Raises OSError like ``open``/``read``.
    """
    wanted = sorted({offset for offset in prefix_offsets if offset is not None})
    if any(offset < 0 for offset in wanted):
        raise ValueError("prefix offsets must be non-negative")
    with open(path, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, f"not a regular file: {path}")
        size = info.st_size
        whole = hashlib.sha256()
        prefixes: dict[int, str] = {}
        if wanted and wanted[0] == 0:
            prefixes[0] = whole.hexdigest()
        line_end = 0
        line_end_hash = hashlib.sha256()
        position = 0
        while position < size:
            chunk = handle.read(min(_READ_CHUNK, size - position))
            if not chunk:
                break
            chunk_start = position
            chunk_end = position + len(chunk)
            for offset in wanted:
                if chunk_start < offset <= chunk_end:
                    prefix_hash = whole.copy()
                    prefix_hash.update(chunk[: offset - chunk_start])
                    prefixes[offset] = prefix_hash.hexdigest()
            terminator_end = _last_terminator_end(chunk)
            if terminator_end > 0:
                line_end_hash = whole.copy()
                line_end_hash.update(chunk[:terminator_end])
                line_end = chunk_start + terminator_end
            whole.update(chunk)
            position = chunk_end
        # A file truncated underneath us is described by what was read.
        size = position
    return TranscriptScan(
        size=size,
        sha256=whole.hexdigest(),
        dev=info.st_dev,
        ino=info.st_ino,
        mtime=info.st_mtime,
        prefix_sha256s={k: v for k, v in prefixes.items() if k <= size},
        line_end=line_end,
        line_end_sha256=line_end_hash.hexdigest(),
    )


class _ByteRange(io.RawIOBase):
    """Read-only raw stream over bytes [start, end) of an open file."""

    def __init__(self, handle, start: int, end: int) -> None:
        super().__init__()
        self._handle = handle
        # Parser warnings label the stream by name, like a file object.
        self.name = getattr(handle, "name", "<transcript range>")
        self._start = start
        self._end = max(start, end)
        self._position = start

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position - self._start

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            target = self._start + offset
        elif whence == io.SEEK_CUR:
            target = self._position + offset
        elif whence == io.SEEK_END:
            target = self._end + offset
        else:
            raise ValueError(f"invalid whence: {whence}")
        self._position = min(max(self._start, target), self._end)
        return self._position - self._start

    def readinto(self, buffer) -> int:
        remaining = self._end - self._position
        if remaining <= 0:
            return 0
        view = memoryview(buffer)[: min(len(buffer), remaining)]
        self._handle.seek(self._position)
        count = self._handle.readinto(view)
        if not count:
            return 0
        self._position += count
        return count


def open_text_range(handle, start: int, end: int) -> io.TextIOWrapper:
    """Text view of bytes [start, end) with parsers.py's decoding rules.

    UTF-8 with replacement and universal newlines, exactly like
    ``open(path, encoding="utf-8", errors="replace")``. ``start`` must sit
    on a line boundary so no record straddles two ranges. The caller keeps
    ``handle`` open for the wrapper's lifetime; closing the wrapper does not
    close it.
    """
    raw = _ByteRange(handle, start, end)
    return io.TextIOWrapper(
        io.BufferedReader(raw, buffer_size=_READ_CHUNK),
        encoding="utf-8",
        errors="replace",
    )
