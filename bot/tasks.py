"""Start background jobs safely.

asyncio only keeps a *weak* reference to tasks made with asyncio.create_task(), so a task that
nothing else holds on to can be garbage-collected and silently stop running. That is exactly what
happened to the reminder checker. spawn() keeps a strong reference until the task finishes, and
logs any task that crashes instead of letting the error disappear.
"""
import asyncio
import logging

log = logging.getLogger("tasks")
_running: set[asyncio.Task] = set()


def spawn(coro, name: str = "") -> asyncio.Task:
    task = asyncio.create_task(coro, name=name or None)
    _running.add(task)
    task.add_done_callback(_finished)
    return task


def _finished(task: asyncio.Task) -> None:
    _running.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        log.error("Background task %s crashed", task.get_name(), exc_info=exc)
