"""Which alliance is the current command/loop working for?  (dependency free on purpose)

One bot process can serve several alliances. The "current alliance" is held in a context variable, so concurrent
Discord commands and background loops never see each other's alliance. With nothing set, the key is "home": the
original single-alliance (TUN) setup, stored and behaving exactly as before.
"""
from __future__ import annotations

import contextlib
import contextvars

HOME = "home"
_current: contextvars.ContextVar = contextvars.ContextVar("tunbank_alliance", default=None)


def current_key() -> str:
    """The key of the alliance in context, or "home" when none is set."""
    return _current.get() or HOME


def current_or_none():
    return _current.get()


@contextlib.contextmanager
def activate(key: str):
    token = _current.set(key)
    try:
        yield key
    finally:
        _current.reset(token)


def set_current(key: str | None):
    """Set for the rest of the current task (used by interaction checks). Returns the token."""
    return _current.set(key)


# The bot installs a function here that maps an interaction to its alliance (raises tenancy.Refusal when it may not proceed).
# With none installed (a plain single-alliance setup, and most tests) buttons and modals simply pass.
_resolver = None


def set_resolver(fn) -> None:
    global _resolver
    _resolver = fn


def resolver():
    return _resolver
