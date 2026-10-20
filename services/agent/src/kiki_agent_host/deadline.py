"""A job's deadline, as a context variable: the tool and model calls a job starts
all see it, and each cuts its own timeout to what is left (agent/guard.py,
agent/executor.py). No SDK imports: the API imports this at startup."""

import contextlib
import time
from collections.abc import Iterator
from contextvars import ContextVar

# In `time.monotonic()` time, so it reads the same from a worker thread
# (`asyncio.to_thread`) as on the event loop; None outside a job with one.
_deadline: ContextVar[float | None] = ContextVar("kiki_deadline", default=None)


@contextlib.contextmanager
def run_deadline(seconds: float | None, *, margin: float = 0) -> Iterator[None]:
    """Gives the code inside (a job, a reply run) `seconds` in all, and no later
    than `margin` before a deadline it runs under. None adds no deadline."""
    if seconds is None:
        yield
        return
    deadline = time.monotonic() + seconds
    if (outer := _deadline.get()) is not None:
        deadline = min(deadline, outer - margin)
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def time_left() -> float | None:
    """Seconds until the deadline, or None outside a job with one."""
    deadline = _deadline.get()
    return None if deadline is None else deadline - time.monotonic()


def within(seconds: float) -> float:
    """A phase's timeout: `seconds`, or what the deadline leaves if that's less
    (never below 0, so `asyncio.timeout(within(...))` fails at once when the
    time is up). For each step of a job: a model request, a tool, a query."""
    left = time_left()
    return max(0.0, seconds if left is None else min(seconds, left))
