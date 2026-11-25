# Path: ClavaBot/CORE/STRATEGY/swing_detector.py
# Role: Детектор свингов (фрактал N свечей слева/справа) и состояния структуры HH/HL/LH/LL без look-ahead

from __future__ import annotations

from collections import deque
from typing import Deque, Optional
from ClavaBot.CORE.config_loader import SwingDetectorConfig
from ClavaBot.CORE.models import Candle, MarketStructureState, RegimeType, SwingKindType, SwingPoint


class SwingDetector:
    """
    Отслеживает локальные экстремумы (свинги) по правилу фрактала без перерисовки:
    свеча на позиции (t - right_candles) подтверждается только после закрытия right_candles справа.
    """

    def __init__(self, symbol: str, cfg: SwingDetectorConfig):
        self.symbol = symbol
        self.left_candles = cfg.left_candles
        self.right_candles = cfg.right_candles
        self.min_swing_sequence = cfg.min_swing_sequence

        win_len = self.left_candles + self.right_candles + 1
        self._window: Deque[Candle] = deque(maxlen=win_len)
        self._bar_index: int = -1
        self._swing_count: int = 0

        self._regime: RegimeType = "NEUTRAL"
        self._last_hh: Optional[SwingPoint] = None
        self._last_hl: Optional[SwingPoint] = None
        self._last_lh: Optional[SwingPoint] = None
        self._last_ll: Optional[SwingPoint] = None
        self._last_swing_high: Optional[SwingPoint] = None
        self._last_swing_low: Optional[SwingPoint] = None

    def update(self, candle: Candle) -> MarketStructureState:
        """
        Принимает закрытую 4H-свечу, обновляет подтверждённые свинги и возвращает снимок
        структуры ДО применения пробойного переключения режима текущей свечой.
        """
        self._bar_index += 1
        self._window.append(candle)

        if len(self._window) == (self.left_candles + self.right_candles + 1):
            self._detect_fractal_at_pivot()

        snapshot = MarketStructureState(
            symbol=self.symbol,
            bar_index=self._bar_index,
            regime=self._regime if self._swing_count >= self.min_swing_sequence else "NEUTRAL",
            last_hh=self._last_hh,
            last_hl=self._last_hl,
            last_lh=self._last_lh,
            last_ll=self._last_ll,
            last_swing_high=self._last_swing_high,
            last_swing_low=self._last_swing_low,
        )

        # Обновление режима после закрытия свечи (защита от повторного сигнала на том же уровне)
        self._advance_regime_on_close(candle.close)
        return snapshot

    def _detect_fractal_at_pivot(self) -> None:
        bars = list(self._window)
        pivot_pos = self.left_candles
        pivot = bars[pivot_pos]
        pivot_bar_idx = self._bar_index - self.right_candles

        left_bars = bars[:pivot_pos]
        right_bars = bars[pivot_pos + 1 :]

        is_high = all(pivot.high > b.high for b in left_bars) and all(pivot.high >= b.high for b in right_bars)
        is_low = all(pivot.low < b.low for b in left_bars) and all(pivot.low <= b.low for b in right_bars)

        if is_high:
            self._register_swing_high(pivot_bar_idx, pivot.close_ts_ms, pivot.high)
        if is_low:
            self._register_swing_low(pivot_bar_idx, pivot.close_ts_ms, pivot.low)

    def _register_swing_high(self, bar_idx: int, ts_ms: int, price: float) -> None:
        self._swing_count += 1
        kind: SwingKindType
        if self._last_swing_high is None:
            kind = "SH_INIT"
        elif price > self._last_swing_high.price:
            kind = "HH"
            self._regime = "BULLISH"
        else:
            kind = "LH"
            if self._regime == "NEUTRAL":
                self._regime = "BEARISH"

        sp = SwingPoint(bar_index=bar_idx, ts_ms=ts_ms, price=price, polarity="HIGH", kind=kind)
        self._last_swing_high = sp
        if kind == "HH":
            self._last_hh = sp
        elif kind == "LH":
            self._last_lh = sp

    def _register_swing_low(self, bar_idx: int, ts_ms: int, price: float) -> None:
        self._swing_count += 1
        kind: SwingKindType
        if self._last_swing_low is None:
            kind = "SL_INIT"
        elif price > self._last_swing_low.price:
            kind = "HL"
            if self._regime == "NEUTRAL":
                self._regime = "BULLISH"
        else:
            kind = "LL"
            self._regime = "BEARISH"

        sp = SwingPoint(bar_index=bar_idx, ts_ms=ts_ms, price=price, polarity="LOW", kind=kind)
        self._last_swing_low = sp
        if kind == "HL":
            self._last_hl = sp
        elif kind == "LL":
            self._last_ll = sp

    def _advance_regime_on_close(self, close_price: float) -> None:
        if self._swing_count < self.min_swing_sequence:
            return
        if self._regime == "BULLISH" and self._last_hl is not None:
            if close_price < self._last_hl.price:
                self._regime = "BEARISH"
        elif self._regime == "BEARISH" and self._last_lh is not None:
            if close_price > self._last_lh.price:
                self._regime = "BULLISH"
