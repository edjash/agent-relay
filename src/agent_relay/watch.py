"""Wake the listener when a file appears in a folder (Linux inotify).

Used to notice an agent's "started" marker straight away, without polling
the folder or ending Gmail's IDLE early. The standard library has no
inotify, so this calls libc directly.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

_IN_CREATE = 0x100
_IN_ATTRIB = 0x004
_IN_MOVED_TO = 0x080
_IN_CLOSE_WRITE = 0x008

_libc = ctypes.CDLL(None, use_errno=True)


class FolderWatch:
    """A file descriptor that becomes readable when a file in `folder` is created, touched or written."""

    def __init__(self, folder: Path):
        fd = _libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        mask = _IN_CREATE | _IN_ATTRIB | _IN_MOVED_TO | _IN_CLOSE_WRITE
        if _libc.inotify_add_watch(fd, os.fsencode(folder), mask) < 0:
            err = ctypes.get_errno()
            os.close(fd)
            raise OSError(err, f"can't watch {folder}")
        self.fd = fd

    def drain(self) -> None:
        """Discard pending events, so select() stops reporting them."""
        try:
            while os.read(self.fd, 4096):
                pass
        except BlockingIOError:
            pass

    def close(self) -> None:
        os.close(self.fd)
