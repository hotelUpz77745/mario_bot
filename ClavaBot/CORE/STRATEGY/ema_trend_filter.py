# Path: ClavaBot/CORE/STRATEGY/ema_trend_filter.py
# Role: Фильтр глобального тренда по EMA (цена > EMA200 для LONG, цена < EMA200 для SHORT)

from __future__ import annotations

from typing import Dict, List
from ClavaBot.CORE.config_loader import EmaTrendFilterConfig
from ClavaBot.CORE.models import Candle, SideType


class EmaTrendFilter:
    """
    Отдельный переиспользуемый фильтр тренда по старшей EMA (по умолчанию EMA 200):
    - LONG разрешён только при close > ema_value;
    - SHORT разрешён только при close < ema_value.
    Обновляется инкрементально за O(1) на каждой закрытой свече.
    """

    def __init__(self, cfg: EmaTrendFilterConfig):
        self.enabled: bool = cfg.enabled
        self.period: int = cfg.period
        if self.period < 1:
            raise ValueError("ema_trend_filter.period must be >= 1")

        self._alpha: float = 2.0 / float(self.period + 1)
        self._one_minus: float = 1.0 - self._alpha
        self._seed_bufs: Dict[str, List[float]] = {}
        self._ema_values: Dict[str, float] = {}
        self._last_closes: Dict[str, float] = {}

    def update(self, symbol: str, candle: Candle) -> None:
        if not self.enabled:
            return

        px = candle.close
        self._last_closes[symbol] = px

        cur_ema = self._ema_values.get(symbol)
        if cur_ema is not None:
            self._ema_values[symbol] = (px * self._alpha) + (cur_ema * self._one_minus)
            return

        buf = self._seed_bufs.setdefault(symbol, [])
        buf.append(px)
        if len(buf) == self.period:
            self._ema_values[symbol] = sum(buf) / float(self.period)
            del self._seed_bufs[symbol]

    def is_allowed(self, symbol: str, side: SideType) -> bool:
        if not self.enabled:
            return True

        ema_val = self._ema_values.get(symbol)
        last_px = self._last_closes.get(symbol)
        if ema_val is None or last_px is None:
            return False

        if side == "LONG":
            return last_px > ema_val
        return last_px < ema_val
