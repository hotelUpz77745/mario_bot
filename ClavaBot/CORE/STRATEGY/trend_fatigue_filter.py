# Path: ClavaBot/CORE/STRATEGY/trend_fatigue_filter.py
# Role: Фильтр усталости тренда (блокировка входа на продолжение после N дней безоткатного движения)

from __future__ import annotations

from collections import deque
from typing import Deque, Dict
from ClavaBot.CORE.candle_aggregator import timeframe_to_ms
from ClavaBot.CORE.config_loader import TrendFatigueConfig
from ClavaBot.CORE.models import Candle, SideType


class TrendFatigueFilter:
    """
    Оценивает окно lookback_days (3 дня): если цена шла направленно с высокой эффективностью
    и без значимого контр-отката (<= max_counter_pullback_ratio от общего размаха),
    считает тренд «уставшим» и блокирует входы на его продолжение.
    """

    def __init__(self, cfg: TrendFatigueConfig, timeframe: str):
        self.enabled = cfg.enabled
        self.lookback_days = cfg.lookback_days
        self.min_net_efficiency_ratio = cfg.min_net_efficiency_ratio
        self.max_counter_pullback_ratio = cfg.max_counter_pullback_ratio
        self.block_mode = cfg.block_mode

        tf_ms = timeframe_to_ms(timeframe)
        bars_per_day = max(1, (86400 * 1000) // tf_ms)
        self.window_bars: int = max(2, self.lookback_days * bars_per_day)
        self._history: Dict[str, Deque[Candle]] = {}

    def update(self, symbol: str, candle: Candle) -> None:
        buf = self._history.get(symbol)
        if buf is None:
            buf = deque(maxlen=self.window_bars)
            self._history[symbol] = buf
        buf.append(candle)

    def is_fatigued(self, symbol: str, proposed_side: SideType) -> bool:
        if not self.enabled:
            return False

        buf = self._history.get(symbol)
        if buf is None or len(buf) < self.window_bars:
            return False

        bars = list(buf)
        hi = max(b.high for b in bars)
        lo = min(b.low for b in bars)
        total_range = hi - lo
        if total_range <= 0.0:
            return False

        net_move = bars[-1].close - bars[0].open
        net_eff_ratio = abs(net_move) / total_range
        if net_eff_ratio < self.min_net_efficiency_ratio:
            return False

        if net_move > 0.0:
            max_pullback = self._max_down_pullback(bars)
            pullback_ratio = max_pullback / total_range
            if pullback_ratio <= self.max_counter_pullback_ratio:
                return proposed_side == "LONG" or self.block_mode == "all_entries"
        elif net_move < 0.0:
            max_bounce = self._max_up_bounce(bars)
            bounce_ratio = max_bounce / total_range
            if bounce_ratio <= self.max_counter_pullback_ratio:
                return proposed_side == "SHORT" or self.block_mode == "all_entries"

        return False

    @staticmethod
    def _max_down_pullback(bars: list[Candle]) -> float:
        running_peak = bars[0].high
        max_dd = 0.0
        for b in bars:
            if b.high > running_peak:
                running_peak = b.high
            dd = running_peak - b.low
            if dd > max_dd:
                max_dd = dd
        return max_dd

    @staticmethod
    def _max_up_bounce(bars: list[Candle]) -> float:
        running_trough = bars[0].low
        max_bounce = 0.0
        for b in bars:
            if b.low < running_trough:
                running_trough = b.low
            bounce = b.high - running_trough
            if bounce > max_bounce:
                max_bounce = bounce
        return max_bounce
