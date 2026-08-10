"""Daily repeating jobs: a count range, a date range, and a time of day.

One schedule fires once per day, at `run_time`, on every day from `start_date`
through `end_date` (inclusive), with a fresh random count between `min_count`
and `max_count` each time.
"""

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Awaitable, Callable

from tasks import (
    ParseError,
    Task,
    parse_count,
    parse_delay,
    parse_link,
    parse_mode,
    parse_platform,
)

log = logging.getLogger(__name__)

DEFAULT_RUN_TIME = time(1, 0)  # 1 AM
POLL_SECONDS = 30

USAGE = "<link> <platform> <min>-<max> <start> <end> [HH:MM] [delay] [mode]"


@dataclass
class Schedule:
    link: str
    platform: str
    min_count: int
    max_count: int
    start_date: date
    end_date: date
    run_time: time
    requested_by: int
    chat_id: int
    delay: float = 0.02
    mode: str = "default"
    id: int = 0
    last_run: str | None = None  # ISO date of the last day it fired
    runs: int = 0

    # --- behaviour -------------------------------------------------------

    def pick_count(self) -> int:
        return random.randint(self.min_count, self.max_count)

    def ran_on(self, day: date) -> bool:
        return self.last_run == day.isoformat()

    def due(self, now: datetime) -> bool:
        """True if today's run is owed and its time has arrived.

        A run whose time passed while the bot was down still fires when the bot
        comes back up, as long as it is the same day.
        """
        today = now.date()
        if not self.start_date <= today <= self.end_date:
            return False
        return not self.ran_on(today) and now.time() >= self.run_time

    def next_run(self, now: datetime) -> datetime | None:
        """When this fires next, or None if it never will again."""
        today = now.date()
        day = max(today, self.start_date)
        while day <= self.end_date:
            if not self.ran_on(day):
                when = datetime.combine(day, self.run_time, tzinfo=now.tzinfo)
                # Overdue today: it goes out on the next scheduler tick.
                return max(when, now) if day == today else when
            day += timedelta(days=1)
        return None

    def to_task(self) -> Task:
        return Task(
            link=self.link,
            platform=self.platform,
            count=self.pick_count(),
            requested_by=self.requested_by,
            delay=self.delay,
            mode=self.mode,
        )

    # --- storage ---------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "link": self.link,
            "platform": self.platform,
            "min_count": self.min_count,
            "max_count": self.max_count,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "run_time": self.run_time.strftime("%H:%M"),
            "requested_by": self.requested_by,
            "chat_id": self.chat_id,
            "delay": self.delay,
            "mode": self.mode,
            "last_run": self.last_run,
            "runs": self.runs,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Schedule":
        return cls(
            link=raw["link"],
            platform=raw["platform"],
            min_count=int(raw["min_count"]),
            max_count=int(raw["max_count"]),
            start_date=date.fromisoformat(raw["start_date"]),
            end_date=date.fromisoformat(raw["end_date"]),
            run_time=datetime.strptime(raw["run_time"], "%H:%M").time(),
            requested_by=int(raw["requested_by"]),
            chat_id=int(raw["chat_id"]),
            delay=float(raw.get("delay", 0.02)),
            mode=raw.get("mode", "default"),
            id=int(raw["id"]),
            last_run=raw.get("last_run"),
            runs=int(raw.get("runs", 0)),
        )

    def count_text(self) -> str:
        if self.min_count == self.max_count:
            return f"{self.min_count:,}"
        return f"{self.min_count:,}-{self.max_count:,}"

    def __str__(self) -> str:
        return (
            f"#{self.id} {self.platform} x{self.count_text()} daily at "
            f"{self.run_time:%H:%M} — {self.link}"
        )


class ScheduleStore:
    """Schedules persisted to a JSON file, so they survive a restart."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()
        self._next_id = 1
        self._schedules: dict[int, Schedule] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (ValueError, OSError):
            log.warning("could not read %s — starting with no schedules", self.path)
            return
        self._next_id = int(data.get("next_id", 1))
        for raw in data.get("schedules", []):
            try:
                sched = Schedule.from_dict(raw)
            except (KeyError, ValueError, TypeError):
                log.warning("skipping unreadable schedule: %s", raw)
                continue
            self._schedules[sched.id] = sched

    def _save(self) -> None:
        payload = {
            "next_id": self._next_id,
            "schedules": [s.to_dict() for s in self.all()],
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self.path)

    def all(self) -> list[Schedule]:
        return sorted(self._schedules.values(), key=lambda s: s.id)

    def get(self, schedule_id: int) -> Schedule | None:
        return self._schedules.get(schedule_id)

    def active(self, today: date) -> list[Schedule]:
        return [s for s in self.all() if today <= s.end_date]

    async def add(self, sched: Schedule) -> Schedule:
        async with self._lock:
            sched.id = self._next_id
            self._next_id += 1
            self._schedules[sched.id] = sched
            self._save()
            return sched

    async def remove(self, schedule_id: int) -> bool:
        """Returns False if there was no such schedule."""
        async with self._lock:
            if schedule_id not in self._schedules:
                return False
            del self._schedules[schedule_id]
            self._save()
            return True

    async def mark_run(self, sched: Schedule, day: date) -> None:
        async with self._lock:
            sched.last_run = day.isoformat()
            sched.runs += 1
            self._save()


# --- Parsing -------------------------------------------------------------

_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d")
_TIME_FORMATS = ("%H:%M", "%H", "%I:%M%p", "%I%p")


def parse_day(raw: str, today: date) -> date:
    """A calendar date: YYYY-MM-DD, DD-MM-YYYY, today, tomorrow, or +N days."""
    text = raw.strip().lower()
    if text == "today":
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)
    if text.startswith("+") and text[1:].isdigit():
        return today + timedelta(days=int(text[1:]))
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    raise ParseError(
        f"'{raw}' is not a date — use YYYY-MM-DD, DD-MM-YYYY, today, tomorrow or +N"
    )


def parse_time_of_day(raw: str) -> time:
    text = raw.strip().lower().replace(" ", "")
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ParseError(f"'{raw}' is not a time — use HH:MM (24-hour), e.g. 01:00")


def parse_count_range(raw: str) -> tuple[int, int]:
    """`500-800` (a range), or `500` (the same count every day)."""
    if "-" in raw:
        low_raw, high_raw = raw.split("-", 1)
        low, high = parse_count(low_raw), parse_count(high_raw)
        if low > high:
            raise ParseError(f"count range '{raw}' is backwards — low first")
        return low, high
    count = parse_count(raw)
    return count, count


def parse_schedule(text: str, requested_by: int, chat_id: int, today: date) -> Schedule:
    """Parse `<link> <platform> <min>-<max> <start> <end> [HH:MM] [delay] [mode]`."""
    parts = text.split()
    if not 5 <= len(parts) <= 8:
        raise ParseError(f"expected: {USAGE}")

    link, platform_raw, count_raw, start_raw, end_raw = parts[:5]
    time_raw = parts[5] if len(parts) >= 6 else None
    delay_raw = parts[6] if len(parts) >= 7 else "0.02"
    mode_raw = parts[7] if len(parts) == 8 else "default"

    min_count, max_count = parse_count_range(count_raw)
    start_date = parse_day(start_raw, today)
    end_date = parse_day(end_raw, today)
    if end_date < start_date:
        raise ParseError("the end date is before the start date")
    if end_date < today:
        raise ParseError("that end date has already passed")

    return Schedule(
        link=parse_link(link),
        platform=parse_platform(platform_raw),
        min_count=min_count,
        max_count=max_count,
        start_date=start_date,
        end_date=end_date,
        run_time=parse_time_of_day(time_raw) if time_raw else DEFAULT_RUN_TIME,
        requested_by=requested_by,
        chat_id=chat_id,
        delay=parse_delay(delay_raw),
        mode=parse_mode(mode_raw),
    )


# --- The loop ------------------------------------------------------------

Fire = Callable[[Schedule, Task], Awaitable[None]]


async def scheduler_loop(
    store: ScheduleStore,
    fire: Fire,
    tz,
    poll_seconds: int = POLL_SECONDS,
) -> None:
    """Check every `poll_seconds` for schedules whose daily run is due."""
    while True:
        try:
            now = datetime.now(tz)
            for sched in store.all():
                if not sched.due(now):
                    continue
                task = sched.to_task()
                # Marked before firing, so a slow or failing send can never
                # make the same day go out twice.
                await store.mark_run(sched, now.date())
                log.info("schedule #%s due — queueing %s clicks", sched.id, task.count)
                try:
                    await fire(sched, task)
                except Exception:  # a bad send must not kill the scheduler
                    log.exception("schedule #%s failed to start", sched.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(poll_seconds)
