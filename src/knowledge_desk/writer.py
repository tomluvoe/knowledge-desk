from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from knowledge_desk.errors import KnowledgeDeskError

LOCK_RELATIVE = "system/.locks/writer.lock"


@contextmanager
def vault_write_lock(vault_root: Path, *, timeout_seconds: float = 30.0) -> Iterator[Path]:
    """Exclusive cross-process lock for canonical write operations.

    v0.1 concurrency model: multi-process writers must serialize through this
    lock. Concurrent readers (validate, search, MCP) do not take the lock.
    """
    vault_root = vault_root.resolve()
    lock_path = vault_root / LOCK_RELATIVE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                _lock_exclusive(handle)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise KnowledgeDeskError(
                        f"timed out waiting for vault write lock at {LOCK_RELATIVE}"
                    ) from exc
                time.sleep(0.05)
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        yield lock_path
    finally:
        try:
            _unlock(handle)
        finally:
            handle.close()


def _lock_exclusive(handle) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows not primary
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
