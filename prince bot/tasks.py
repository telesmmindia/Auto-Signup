
"""Task parsing, the job queue, and the platform handler registry."""

import asyncio
import itertools
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

# Platform aliases -> canonical name. Add new platforms here.
PLATFORMS: dict[str, str] = {
    "telegram": "telegram",
    "tg": "telegram",
    "instagram": "instagram",
    "insta": "instagram",
    "ig": "instagram",
    "youtube": "youtube",
    "yt": "youtube",
    "tiktok": "tiktok",
    "tt": "tiktok",
    "twitter": "twitter",
    "x": "twitter",
    "facebook": "facebook",
    "fb": "facebook",
    "discord": "discord",
    "dc": "discord",
    "kick": "kick",
    "twitch": "twitch",
    "ttv": "twitch",
    "whatsapp": "whatsapp",
    "wa": "whatsapp",
}

PLATFORM_URLS: dict[str, str] = {
    "telegram": "https://t.me/",
    "instagram": "https://www.instagram.com/",
    "youtube": "https://www.youtube.com/",
    "tiktok": "https://www.tiktok.com/",
    "twitter": "https://twitter.com/",
    "facebook": "https://www.facebook.com/",
    "discord": "https://discord.com/",
    "kick": "https://kick.com/",
    "twitch": "https://www.twitch.tv/",
    "whatsapp": "https://web.whatsapp.com/",
}
MAX_COUNT = 1_000_000

_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_counter = itertools.count(1)


class ParseError(ValueError):
    """Raised when a submitted line is not a valid task."""


@dataclass
class Task:
    link: str
    platform: str
    count: int
    requested_by: int
    delay: float = 0.02
    mode: str = "default"
    id: int = field(default_factory=lambda: next(_counter))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "queued"

    def __str__(self) -> str:
        return f"#{self.id} {self.platform} x{self.count} — {self.link}"


def parse_task(text: str, requested_by: int) -> Task:
    """Parse one line of `<link> <platform> <count> [delay] [mode]`.
    
    platform: platform name or "random" for random platform each request
    delay: optional, seconds between each request (default: 0.02)
    mode: optional, "random" for random referers, "default" for platform referer (default: "default")
    """
    parts = text.split()
    if len(parts) < 3 or len(parts) > 5:
        raise ParseError("expected 3-5 parts: <link> <platform> <count> [delay] [mode]")

    link, platform_raw, count_raw = parts[:3]
    delay_raw = parts[3] if len(parts) >= 4 else "0.02"
    mode_raw = parts[4] if len(parts) == 5 else "default"

    if not _URL_RE.match(link):
        raise ParseError(f"'{link}' is not a valid http(s) link")

    # Handle "random" platform
    if platform_raw.lower() == "random":
        platform = "random"
    else:
        platform = PLATFORMS.get(platform_raw.lower())
        if platform is None:
            raise ParseError(f"unknown platform '{platform_raw}'")

    try:
        count = int(count_raw.replace(",", "").replace("_", ""))
    except ValueError:
        raise ParseError(f"'{count_raw}' is not a number") from None
    if not 1 <= count <= MAX_COUNT:
        raise ParseError(f"count must be between 1 and {MAX_COUNT:,}")

    try:
        delay = float(delay_raw)
    except ValueError:
        raise ParseError(f"'{delay_raw}' is not a valid delay (seconds)") from None
    if not 0 <= delay <= 3600:
        raise ParseError("delay must be between 0 and 3600 seconds")

    if mode_raw not in ("default", "random", "variety"):
        raise ParseError("mode must be 'default', 'random', or 'variety'")

    return Task(link=link, platform=platform, count=count, requested_by=requested_by, delay=delay, mode=mode_raw)


def parse_batch(text: str, requested_by: int) -> tuple[list[Task], list[tuple[str, str]]]:
    """Parse every non-empty line. Returns (tasks, [(line, error), ...])."""
    tasks: list[Task] = []
    errors: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            tasks.append(parse_task(line, requested_by))
        except ParseError as exc:
            errors.append((line, str(exc)))
    return tasks, errors


# --- Handler registry ----------------------------------------------------
# A handler receives the Task and a `progress` coroutine it may call with a
# short status string. Register one per platform with @handler("name").

Progress = Callable[[str], Awaitable[None]]
Handler = Callable[[Task, Progress], Awaitable[str]]

HANDLERS: dict[str, Handler] = {}


def handler(*platforms: str) -> Callable[[Handler], Handler]:
    def decorator(fn: Handler) -> Handler:
        for name in platforms:
            HANDLERS[name] = fn
        return fn

    return decorator


async def run_task(task: Task, progress: Progress) -> str:
    fn = HANDLERS.get(task.platform)
    if fn is None:
        raise RuntimeError(f"no handler registered for '{task.platform}'")
    return await fn(task, progress)


# --- Queue ---------------------------------------------------------------

queue: asyncio.Queue[tuple[Task, Progress, Callable[[Task, str | None, str | None], Awaitable[None]]]] = asyncio.Queue()


async def worker(name: str) -> None:
    """Pull tasks off the queue and run them until cancelled."""
    while True:
        task, progress, done = await queue.get()
        try:
            task.status = "running"
            result = await run_task(task, progress)
            task.status = "done"
            await done(task, result, None)
        except asyncio.CancelledError:
            task.status = "cancelled"
            raise
        except Exception as exc:  # a bad handler must not kill the worker
            log.exception("task %s failed", task.id)
            task.status = "failed"
            await done(task, None, str(exc))
        finally:
            queue.task_done()
