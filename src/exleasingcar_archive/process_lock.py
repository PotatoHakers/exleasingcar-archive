"""Portable advisory lock for preventing overlapping collector processes.

The lock is held by the operating system and released automatically when the
process exits, including after an unexpected termination. The lock file itself
is intentionally retained; deleting an advisory-lock file can create races
with another process opening the same path.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import BinaryIO

from typing_extensions import Self


class CollectorAlreadyRunning(RuntimeError):
    """Raised when another process already owns the collector lock."""


class CollectorProcessLock:
    """Cross-platform, non-blocking, single-process lock backed by a file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle: BinaryIO | None = None

    def acquire(self) -> CollectorProcessLock:
        """Acquire the lock or fail immediately when another process owns it."""
        if self._handle is not None:
            raise RuntimeError("collector process lock is already acquired")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)

            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(
                exc, "winerror", None
            ) == 33:
                raise CollectorAlreadyRunning(
                    f"Another collector process already holds the lock: {self.path}"
                ) from exc
            raise

        self._handle = handle
        return self

    def release(self) -> None:
        """Release the OS lock and close its file handle without deleting the file."""
        handle = self._handle
        if handle is None:
            return

        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self.release()
        return False
