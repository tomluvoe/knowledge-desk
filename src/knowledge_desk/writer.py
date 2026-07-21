from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from knowledge_desk.errors import KnowledgeDeskError

LOCK_RELATIVE = "system/.locks/writer.lock"
_LOCK_STATE = threading.local()


@contextmanager
def vault_write_lock(vault_root: Path, *, timeout_seconds: float = 30.0) -> Iterator[Path]:
    """Exclusive cross-process lock for canonical write operations.

    v0.1 concurrency model: multi-process writers must serialize through this
    lock. Concurrent readers (validate, search, MCP) do not take the lock.
    """
    vault_root = vault_root.resolve()
    key = str(vault_root)
    active = _active_locks()
    if key in active:
        lock_path, depth = active[key]
        active[key] = (lock_path, depth + 1)
        try:
            yield lock_path
        finally:
            active[key] = (lock_path, depth)
        return

    backend = _load_fcntl()
    lock_path = vault_root / LOCK_RELATIVE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                _lock_exclusive(handle, backend)
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
        os.fsync(handle.fileno())
        active[key] = (lock_path, 1)
        try:
            yield lock_path
        finally:
            active.pop(key, None)
    finally:
        try:
            _unlock(handle, backend)
        finally:
            handle.close()


def vault_write_lock_held(vault_root: Path) -> bool:
    return str(vault_root.resolve()) in _active_locks()


def _active_locks() -> dict[str, tuple[Path, int]]:
    active = getattr(_LOCK_STATE, "active", None)
    if active is None:
        active = {}
        _LOCK_STATE.active = active
    return active


def _load_fcntl():
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - exercised via injected failure
        raise KnowledgeDeskError(
            "cross-process vault writer locking is unavailable on this platform"
        ) from exc
    return fcntl


def _lock_exclusive(handle, backend) -> None:
    backend.flock(handle.fileno(), backend.LOCK_EX | backend.LOCK_NB)


def _unlock(handle, backend) -> None:
    try:
        backend.flock(handle.fileno(), backend.LOCK_UN)
    except OSError:
        pass
