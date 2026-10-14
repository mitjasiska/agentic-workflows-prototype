"""Entry-point identity for commands authored by AWL, never user-text rewriting."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps


_name = ContextVar('awl_command_name', default='task')


def command_name():
    return _name.get()


@contextmanager
def command_scope(name):
    token = _name.set(name)
    try:
        yield
    finally:
        _name.reset(token)


def bind_command(reader):
    """Keep the creating command's identity in dashboard worker/request threads."""
    name = command_name()

    @wraps(reader)
    def bound(*args, **kwargs):
        with command_scope(name):
            return reader(*args, **kwargs)

    return bound
