"""APFS copy-on-write primitives: clonefile(2), clone identity, rename swaps.

Everything here degrades to "unsupported" off macOS so callers can fall back
to byte copies.  Nothing in this module decides policy (modes, managed roots,
atomic replacement); `logpile.sync` owns that.
"""

from __future__ import annotations

import ctypes
import errno
import os
import struct
import sys
from dataclasses import dataclass
from functools import cache

# <sys/clonefile.h>.  CLONE_ACL ("copy ACLs from the source file", per
# clonefile(2)) is deliberately never passed, so a clone does not carry the
# source's ACL (tests/test_clone_storage.py checks this on macOS).  A clone
# does inherit the destination directory's inheritable ACEs, exactly as a
# newly created byte copy would; logpile.sync keeps no clone that ends up with
# any extended ACL and byte-copies instead.
CLONE_NOOWNERCOPY = 0x0002

# errno values meaning "this volume or pair of paths cannot clone"; callers
# fall back to a byte copy.  Everything else is a real failure.  clonefile(2)
# documents EINVAL only for an invalid flags value; it stays on this list
# because the storage design treats it as "cannot clone here", and the macOS
# tests that forbid the byte-copy fallback catch a flags regression.
CLONE_UNSUPPORTED_ERRNOS = frozenset(
    {errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.ENOSYS, errno.EINVAL}
)

# <sys/stdio.h>: renamex_np(2) flag that atomically exchanges two paths.
RENAME_SWAP = 0x00000002
# errno values meaning "this volume cannot swap"; callers fall back to a
# plain rename.  rename(2) documents EINVAL for invalid flags, so it is a
# real failure here.
SWAP_UNSUPPORTED_ERRNOS = frozenset({errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS})

# <sys/attr.h>
_ATTR_BIT_MAP_COUNT = 5
_ATTR_CMN_RETURNED_ATTRS = 0x80000000
# ATTR_CMNEXT_* live in the forkattr field when FSOPT_ATTR_CMN_EXTENDED is set.
_ATTR_CMNEXT_PRIVATESIZE = 0x00000008
_ATTR_CMNEXT_REALFSID = 0x00000080
_ATTR_CMNEXT_CLONEID = 0x00000100
_ATTR_CMNEXT_EXT_FLAGS = 0x00000200
_FSOPT_NOFOLLOW = 0x00000001
_FSOPT_ATTR_CMN_EXTENDED = 0x00000020

# <sys/stat.h> extended flags (ATTR_CMNEXT_EXT_FLAGS).
EF_SHARES_ALL_BLOCKS = 0x00000040

# <sys/acl.h>
_ACL_TYPE_EXTENDED = 0x00000100


class CloneUnsupported(Exception):
    """clonefile(2) is unavailable here or cannot clone between these paths.

    Deliberately not an OSError, so generic OSError handlers never mistake an
    expected fallback for a storage failure (or the reverse).
    """

    def __init__(self, message: str, errno_value: int | None = None) -> None:
        super().__init__(message)
        self.errno = errno_value


class SwapUnsupported(Exception):
    """renamex_np(RENAME_SWAP) is unavailable here or unsupported by the volume."""

    def __init__(self, message: str, errno_value: int | None = None) -> None:
        super().__init__(message)
        self.errno = errno_value


@dataclass(frozen=True)
class FileStorage:
    """What APFS reports about one file's data stream (None: not reported).

    ``private_size`` is ATTR_CMNEXT_PRIVATESIZE: the bytes "not trapped inside
    a clone or snapshot", which deleting the file would free immediately.  An
    APFS snapshot (for example a Time Machine local snapshot) traps the blocks
    of every file written before it, so an old byte copy reports 0 while that
    snapshot exists.  It measures immediate reclaim, never clone status.

    ``clone_id`` is ATTR_CMNEXT_CLONEID, which getattrlist(2) documents as
    uniquely identifying the file's data stream: "pure clones of each other"
    share it.  Snapshots do not change it.  ``fsid`` is ATTR_CMNEXT_REALFSID,
    the real volume, which tells the sealed system volume apart from the data
    volume even where both report the same st_dev.
    """

    fsid: tuple[int, int] | None = None
    clone_id: int | None = None
    private_size: int | None = None
    ext_flags: int | None = None

    def is_clone_of(self, other: FileStorage) -> bool:
        """Whether both files are the same data stream on the same volume.

        True means the two already share every data block, so recloning one
        from the other frees nothing.  Unknown values never count as a match:
        the caller then verifies and reclones, which is always safe.
        """
        return (
            self.fsid is not None
            and self.fsid == other.fsid
            and bool(self.clone_id)
            and self.clone_id == other.clone_id
            and self.ext_flags is not None
            and bool(self.ext_flags & EF_SHARES_ALL_BLOCKS)
        )


class _AttrList(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


@cache
def _libc():
    if sys.platform != "darwin":
        return None
    try:
        return ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None


def _symbol(name: str, argtypes: tuple, restype):
    libc = _libc()
    if libc is None:
        return None
    try:
        function = getattr(libc, name)
    except AttributeError:
        return None
    function.argtypes = argtypes
    function.restype = restype
    return function


@cache
def _clonefile_function():
    # <sys/clonefile.h>: int clonefile(const char *, const char *, uint32_t)
    return _symbol(
        "clonefile",
        (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32),
        ctypes.c_int,
    )


@cache
def _renamex_function():
    # <sys/stdio.h>: int renamex_np(const char *, const char *, unsigned int)
    return _symbol(
        "renamex_np",
        (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint),
        ctypes.c_int,
    )


@cache
def _getattrlist_function():
    return _symbol(
        "getattrlist",
        (
            ctypes.c_char_p,
            ctypes.POINTER(_AttrList),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
        ),
        ctypes.c_int,
    )


@cache
def _acl_functions():
    get_fd = _symbol("acl_get_fd_np", (ctypes.c_int, ctypes.c_int), ctypes.c_void_p)
    free = _symbol("acl_free", (ctypes.c_void_p,), ctypes.c_int)
    if get_fd is None or free is None:
        return None
    return get_fd, free


def clone_available() -> bool:
    """Whether this process can call clonefile(2) at all."""
    return _clonefile_function() is not None


def clonefile(src: os.PathLike | str, dst: os.PathLike | str) -> None:
    """Clone ``src`` (following symlinks) to the new path ``dst``.

    Raises CloneUnsupported when the platform, volume, or path pair cannot
    clone, FileExistsError when ``dst`` already exists, and OSError for every
    other failure.  clonefile(2) is atomic: when it fails, it created nothing.
    """
    function = _clonefile_function()
    if function is None:
        raise CloneUnsupported("clonefile(2) is unavailable on this platform")
    src_bytes = os.fsencode(src)
    dst_bytes = os.fsencode(dst)
    while True:
        ctypes.set_errno(0)
        if function(src_bytes, dst_bytes, CLONE_NOOWNERCOPY) == 0:
            return
        error = ctypes.get_errno()
        if error != errno.EINTR:
            break
    if error in CLONE_UNSUPPORTED_ERRNOS:
        raise CloneUnsupported(
            f"clonefile(2) cannot clone {os.fspath(src)!r}: {os.strerror(error)}",
            error,
        )
    raise OSError(error, os.strerror(error), os.fspath(src), None, os.fspath(dst))


def rename_swap(first: os.PathLike | str, second: os.PathLike | str) -> None:
    """Atomically exchange two existing paths (renamex_np RENAME_SWAP).

    Unlike rename(2), this never creates ``second``: it raises
    FileNotFoundError when either path is missing.  Raises SwapUnsupported
    off macOS or on a volume without swap support, and OSError otherwise.
    """
    function = _renamex_function()
    if function is None:
        raise SwapUnsupported("renamex_np(2) is unavailable on this platform")
    first_bytes = os.fsencode(first)
    second_bytes = os.fsencode(second)
    while True:
        ctypes.set_errno(0)
        if function(first_bytes, second_bytes, RENAME_SWAP) == 0:
            return
        error = ctypes.get_errno()
        if error != errno.EINTR:
            break
    if error in SWAP_UNSUPPORTED_ERRNOS:
        raise SwapUnsupported(
            f"renamex_np(2) cannot swap {os.fspath(first)!r}: {os.strerror(error)}",
            error,
        )
    raise OSError(error, os.strerror(error), os.fspath(first), None, os.fspath(second))


def file_storage(path: os.PathLike | str) -> FileStorage | None:
    """APFS data-stream facts for ``path`` (symlinks not followed).

    Returns None off macOS or on any lookup error; fields the volume does not
    report are None.  Callers must treat None as "no information".
    """
    function = _getattrlist_function()
    if function is None:
        return None
    request = _AttrList(bitmapcount=_ATTR_BIT_MAP_COUNT)
    request.commonattr = _ATTR_CMN_RETURNED_ATTRS
    request.forkattr = (
        _ATTR_CMNEXT_PRIVATESIZE
        | _ATTR_CMNEXT_REALFSID
        | _ATTR_CMNEXT_CLONEID
        | _ATTR_CMNEXT_EXT_FLAGS
    )
    buffer = ctypes.create_string_buffer(128)
    result = function(
        os.fsencode(path),
        ctypes.byref(request),
        buffer,
        ctypes.sizeof(buffer),
        _FSOPT_NOFOLLOW | _FSOPT_ATTR_CMN_EXTENDED,
    )
    if result != 0:
        return None
    return _parse_file_storage(buffer.raw)


def _parse_file_storage(raw: bytes) -> FileStorage | None:
    """Decode a getattrlist(2) reply for the attributes file_storage asks for.

    Layout: u_int32_t length, the attribute_set_t of attributes actually
    returned (ATTR_CMN_RETURNED_ATTRS), then each returned attribute in
    bit order, packed at 4-byte alignment.
    """
    header = struct.Struct("=I5I")
    if len(raw) < header.size:
        return None
    length, _common, _vol, _dir, _file, returned = header.unpack_from(raw, 0)
    offset = header.size
    values: dict[str, object] = {}
    for bit, name, layout in (
        (_ATTR_CMNEXT_PRIVATESIZE, "private_size", "=q"),
        (_ATTR_CMNEXT_REALFSID, "fsid", "=2i"),
        (_ATTR_CMNEXT_CLONEID, "clone_id", "=Q"),
        (_ATTR_CMNEXT_EXT_FLAGS, "ext_flags", "=Q"),
    ):
        if not returned & bit:
            continue
        size = struct.calcsize(layout)
        if offset + size > min(length, len(raw)):
            return None
        unpacked = struct.unpack_from(layout, raw, offset)
        values[name] = unpacked if name == "fsid" else unpacked[0]
        offset += size
    private = values.get("private_size")
    if private is not None and private < 0:
        values["private_size"] = None
    return FileStorage(**values)


def private_size(path: os.PathLike | str) -> int | None:
    """Bytes of ``path`` that deleting it would free now, or None if unknown.

    See FileStorage.private_size: blocks shared with a clone *or trapped in
    a snapshot* are excluded, so 0 does not mean "already a clone".
    """
    storage = file_storage(path)
    return None if storage is None else storage.private_size


def has_extended_acl(fd: int) -> bool:
    """Whether the open file carries an extended (NFSv4-style) ACL.

    False off macOS or on a volume without ACL support.  Raises OSError for
    any other failure, so a caller guarding confidentiality fails closed
    instead of assuming "no ACL".
    """
    functions = _acl_functions()
    if functions is None:
        return False
    get_fd, free = functions
    ctypes.set_errno(0)
    acl = get_fd(fd, _ACL_TYPE_EXTENDED)
    if acl:
        free(acl)
        return True
    error = ctypes.get_errno()
    # ENOENT: no extended ACL.  ENOTSUP/EOPNOTSUPP: the volume has no ACLs.
    if error in {errno.ENOENT, errno.ENOTSUP, errno.EOPNOTSUPP}:
        return False
    raise OSError(error, f"Cannot read ACL: {os.strerror(error)}")
