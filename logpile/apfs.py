"""APFS copy-on-write primitives: clonefile(2) and private-size queries.

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
from functools import cache

# <sys/clonefile.h>.  CLONE_ACL ("copy ACLs from the source file", per
# clonefile(2)) is deliberately never passed, so a clone should not carry the
# source's ACL (tests/test_clone_storage.py checks this on macOS).  logpile.sync
# still refuses to keep any clone that ends up with an extended ACL, whatever
# its origin, and byte-copies instead.
CLONE_NOOWNERCOPY = 0x0002

# errno values meaning "this volume or pair of paths cannot clone"; callers
# fall back to a byte copy.  Everything else is a real failure.
CLONE_UNSUPPORTED_ERRNOS = frozenset(
    {errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.ENOSYS, errno.EINVAL}
)

# <sys/attr.h>
_ATTR_BIT_MAP_COUNT = 5
_ATTR_CMNEXT_PRIVATESIZE = 0x00000008
_FSOPT_NOFOLLOW = 0x00000001
_FSOPT_ATTR_CMN_EXTENDED = 0x00000020

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
    return _symbol(
        "clonefile", (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int), ctypes.c_int
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


def private_size(path: os.PathLike | str) -> int | None:
    """Bytes of ``path`` not shared with any other file, or None if unknown.

    APFS reports this as ATTR_CMNEXT_PRIVATESIZE: a byte copy's private size
    is its whole allocation, and a fresh clone's is zero.  Symlinks are not
    followed.  Returns None off macOS, on volumes without the attribute, or
    on any lookup error, so callers must treat None as "no information".
    """
    function = _getattrlist_function()
    if function is None:
        return None
    request = _AttrList(bitmapcount=_ATTR_BIT_MAP_COUNT)
    request.forkattr = _ATTR_CMNEXT_PRIVATESIZE
    buffer = ctypes.create_string_buffer(64)
    result = function(
        os.fsencode(path),
        ctypes.byref(request),
        buffer,
        ctypes.sizeof(buffer),
        _FSOPT_NOFOLLOW | _FSOPT_ATTR_CMN_EXTENDED,
    )
    if result != 0:
        return None
    # u_int32_t total length, then the off_t attribute at 4-byte alignment.
    length, value = struct.unpack_from("=Iq", buffer.raw, 0)
    if length < struct.calcsize("=Iq") or value < 0:
        return None
    return value


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
