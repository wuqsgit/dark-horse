from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping


def parse_time(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class EventClock:
    """Yields closed-bar evaluation times in strict event-time order."""

    interval_minutes: int = 15

    def ticks(self, rows: Iterable[Mapping]) -> list[datetime]:
        close_times = {
            parse_time(row["time"]) + timedelta(minutes=self.interval_minutes)
            for row in rows or []
            if row.get("time") is not None
            and int(row.get("is_closed", 1) or 0) == 1
        }
        return sorted(close_times)
