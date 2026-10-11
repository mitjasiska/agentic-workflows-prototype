"""Short machine-local ownership transactions, never agent-turn ownership.

Lock order: try the existing private Git publication lock without waiting, then
(if needed) take the permanent checkout lock for shared Git administration.
Only that repository lock may wait, and only outside the registry gate. Its
holder never waits for a task lock. The gate then protects inventory/admission
or a local topology change; SQLite write transactions come last. Selection may also try a resource lock under
the gate, but must NEVER wait for it. This prevents an inverted-order deadlock.
Release the gate before agent/provider waits or recursive filesystem deletion.
A disposal publishes its durable reservation under the exclusive gate before
releasing it; claim checks and writes share the gate as one transaction.
"""

from contextlib import contextmanager
from functools import wraps
from inspect import signature
import os
from pathlib import Path
import threading
import time

from . import TaskError


_held = threading.local()


@contextmanager
def ownership_gate(*, exclusive=False, registry=None, timeout=30):
    from .contexts import registry_path
    path = (Path(registry) if registry is not None else registry_path()).parent / "ownership.lock"
    path = path.absolute()
    key = (os.getpid(), str(path))
    held = getattr(_held, "locks", None)
    if held is None:
        held = _held.locks = {}
    if key in held:
        if exclusive and not held[key]:
            raise TaskError("Cannot upgrade a shared registry transaction; release it before disposal admission")
        yield
        return
    fd = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.resolve() != path:
            raise TaskError("Workflow ownership lock path is aliased")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        import fcntl
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TaskError("Workflow registry is busy with a short ownership transaction; retry shortly. "
                                    "If it persists, inspect the controller doing registry/Git administration; "
                                    "do not delete locks, journals or context history") from None
                time.sleep(0.01)
        held[key] = exclusive
        try:
            yield
        finally:
            del held[key]
    except OSError:
        raise TaskError("Cannot lock machine-local workflow ownership; no mutation is safe") from None
    finally:
        if fd is not None:
            os.close(fd)


def ownership_operation(function=None, *, exclusive=False):
    def decorate(fn):
        parameters = signature(fn)
        @wraps(fn)
        def guarded(*args, **kwargs):
            registry = parameters.bind_partial(*args, **kwargs).arguments.get("registry")
            with ownership_gate(exclusive=exclusive, registry=registry.path if registry is not None else None):
                return fn(*args, **kwargs)
        return guarded
    return decorate(function) if function is not None else decorate
