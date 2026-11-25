# Path: ClavaBot/CORE/STRATEGY/session_window_filter.py
# Role: Фильтр активного торгового окна Нью-Йорка (08:00–16:00 EST / America/New_York)

from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
from ClavaBot.CORE.config_loader import SessionWindowConfig


def _parse_hhmm(raw: str) -> time:
    parts = raw.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Expected HH:MM time format, got '{raw}'")
    return time(hour=int(parts[0]), minute=int(parts[1]))


class SessionWindowFilter:
    """
    Разрешает открытие новых позиций строго в активную сессию Нью-Йорка.
    """

    def __init__(self, cfg: SessionWindowConfig):
        self.enabled = cfg.enabled
        self.tz = ZoneInfo(cfg.timezone)
        self.start_time = _parse_hhmm(cfg.start_time)
        self.end_time = _parse_hhmm(cfg.end_time)
        self.weekdays_only = cfg.weekdays_only

    def is_allowed(self, ts_ms: int) -> bool:
        if not self.enabled:
            return True

        local_dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).astimezone(self.tz)
        if self.weekdays_only and local_dt.weekday() >= 5:
            return False

        cur_t = local_dt.time()
        return self.start_time <= cur_t <= self.end_time
