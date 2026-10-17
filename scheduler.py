"""Small self-managed daily scheduler used for maintenance jobs."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

LOG = logging.getLogger("astrbot_plugin_echo_cave")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_daily_time(value: str) -> tuple[int, int] | None:
    match = _TIME_RE.match((value or "").strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour, minute


def seconds_until_next_run(hour: int, minute: int, now: datetime | None = None) -> float:
    current = now or datetime.now().astimezone()
    next_run = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_run <= current:
        next_run = next_run + timedelta(days=1)
    return max(0.0, (next_run - current).total_seconds())


class DailyScheduler:
    """Runs callbacks once per day at a local ``HH:mm`` time.

    A single asyncio task per job is created; ``stop()`` cancels everything.
    Invalid times are logged and skipped.
    """

    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._tasks: dict[str, asyncio.Task] = {}

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def add(
        self,
        name: str,
        time_value: str,
        callback: Callable[[], Awaitable[None]],
    ) -> bool:
        parsed = parse_daily_time(time_value)
        if parsed is None:
            LOG.warning("Invalid %s time %r. Expected HH:mm.", name, time_value)
            return False
        if name in self._tasks:
            self._tasks[name].cancel()
        self._tasks[name] = asyncio.create_task(self._loop(name, parsed[0], parsed[1], callback))
        return True

    async def _loop(
        self,
        name: str,
        hour: int,
        minute: int,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        while not self._stop.is_set():
            delay = seconds_until_next_run(hour, minute)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                return
            except Exception as exc:
                LOG.error("Scheduled task %s failed: %s", name, exc)

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks.values():
            task.cancel()
        for task in list(self._tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
