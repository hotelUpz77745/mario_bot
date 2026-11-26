# Path: MarioBot/CORE/STRATEGY/_indicators.py
# Role: Переиспользуемые инкрементальные индикаторы (EMA, SMA, ATR, Bollinger, Donchian, Stoch, MACD)

from __future__ import annotations

import math
from collections import deque
from typing import Deque, Optional, Tuple
from MarioBot.CORE.models import Candle


class IncrementalEMA:
    """Инкрементальная EMA O(1) с SMA-инициализацией первых `period` значений."""

    def __init__(self, period: int):
        if period < 1:
            raise ValueError("EMA period must be >= 1")
        self.period: int = int(period)
        self._alpha: float = 2.0 / float(self.period + 1)
        self._one_minus: float = 1.0 - self._alpha
        self._seed: list[float] = []
        self.value: Optional[float] = None

    def update(self, price: float) -> Optional[float]:
        if self.value is not None:
            self.value = (price * self._alpha) + (self.value * self._one_minus)
            return self.value
        self._seed.append(price)
        if len(self._seed) == self.period:
            self.value = sum(self._seed) / float(self.period)
            self._seed.clear()
        return self.value


class IncrementalSMA:
    """Скользящая простая средняя O(1) с поддержкой окна последних значений."""

    def __init__(self, period: int):
        if period < 1:
            raise ValueError("SMA period must be >= 1")
        self.period: int = int(period)
        self._buf: Deque[float] = deque(maxlen=self.period)
        self._sum: float = 0.0
        self.value: Optional[float] = None

    def update(self, price: float) -> Optional[float]:
        if len(self._buf) == self.period:
            self._sum -= self._buf[0]
        self._buf.append(price)
        self._sum += price
        if len(self._buf) == self.period:
            self.value = self._sum / float(self.period)
        return self.value


class IncrementalATR:
    """Инкрементальный Wilder ATR O(1) с SMA-инициализацией первых `period` True Range."""

    def __init__(self, period: int):
        if period < 1:
            raise ValueError("ATR period must be >= 1")
        self.period: int = int(period)
        self._prev_close: Optional[float] = None
        self._seed: list[float] = []
        self.value: Optional[float] = None

    def update(self, candle: Candle) -> Optional[float]:
        if self._prev_close is None:
            tr = candle.high - candle.low
        else:
            tr = max(
                candle.high - candle.low,
                abs(candle.high - self._prev_close),
                abs(candle.low - self._prev_close),
            )
        self._prev_close = candle.close

        if self.value is not None:
            self.value = ((self.value * float(self.period - 1)) + tr) / float(self.period)
            return self.value

        self._seed.append(tr)
        if len(self._seed) == self.period:
            self.value = sum(self._seed) / float(self.period)
            self._seed.clear()
        return self.value


class RollingBollinger:
    """Полосы Боллинджера на скользящем окне `period` с множителем `std_mult`."""

    def __init__(self, period: int, std_mult: float):
        if period < 2:
            raise ValueError("Bollinger period must be >= 2")
        self.period: int = int(period)
        self.std_mult: float = float(std_mult)
        self._closes: Deque[float] = deque(maxlen=self.period)

    def update(self, close: float) -> Optional[Tuple[float, float, float]]:
        self._closes.append(close)
        if len(self._closes) < self.period:
            return None
        n = float(self.period)
        mean = sum(self._closes) / n
        var = sum((x - mean) ** 2 for x in self._closes) / n
        std = math.sqrt(var)
        return (mean + self.std_mult * std, mean, mean - self.std_mult * std)


class RollingStochastic:
    """Стохастик (%K, %D) на окне `k_period` со сглаживанием `k_smooth` и `d_smooth`."""

    def __init__(self, k_period: int, k_smooth: int, d_smooth: int):
        self._highs: Deque[float] = deque(maxlen=k_period)
        self._lows: Deque[float] = deque(maxlen=k_period)
        self._k_sma = IncrementalSMA(k_smooth)
        self._d_sma = IncrementalSMA(d_smooth)
        self.k_period = k_period

    def update(self, candle: Candle) -> Optional[Tuple[float, float]]:
        self._highs.append(candle.high)
        self._lows.append(candle.low)
        if len(self._highs) < self.k_period:
            return None
        hi = max(self._highs)
        lo = min(self._lows)
        raw_k = 50.0 if hi <= lo else ((candle.close - lo) / (hi - lo)) * 100.0
        k_val = self._k_sma.update(raw_k)
        if k_val is None:
            return None
        d_val = self._d_sma.update(k_val)
        if d_val is None:
            return None
        return k_val, d_val


class IncrementalMACD:
    """MACD (fast, slow, signal) с SMA/EMA расчётом."""

    def __init__(self, fast: int, slow: int, signal: int):
        self._fast = IncrementalEMA(fast)
        self._slow = IncrementalEMA(slow)
        self._sig = IncrementalEMA(signal)

    def update(self, price: float) -> Optional[Tuple[float, float]]:
        f = self._fast.update(price)
        s = self._slow.update(price)
        if f is None or s is None:
            return None
        macd = f - s
        sig = self._sig.update(macd)
        if sig is None:
            return None
        return macd, sig


class IncrementalADX:
    """Инкрементальный индекс направленного движения Уайлдера (ADX) O(1) на свече."""

    def __init__(self, period: int = 14):
        if period < 2:
            raise ValueError("ADX period must be >= 2")
        self.period: int = int(period)
        self._prev: Optional[Candle] = None
        self._tr_sum: float = 0.0
        self._pdm_sum: float = 0.0
        self._ndm_sum: float = 0.0
        self._count: int = 0
        self._dx_seed: list[float] = []
        self.value: Optional[float] = None

    def update(self, candle: Candle) -> Optional[float]:
        if self._prev is None:
            self._prev = candle
            return None
        tr = max(
            candle.high - candle.low,
            abs(candle.high - self._prev.close),
            abs(candle.low - self._prev.close),
        )
        up_move = candle.high - self._prev.high
        down_move = self._prev.low - candle.low
        pdm = up_move if (up_move > down_move and up_move > 0.0) else 0.0
        ndm = down_move if (down_move > up_move and down_move > 0.0) else 0.0
        self._prev = candle

        if self._count < self.period:
            self._tr_sum += tr
            self._pdm_sum += pdm
            self._ndm_sum += ndm
            self._count += 1
            if self._count < self.period:
                return None
        else:
            p = float(self.period)
            self._tr_sum = self._tr_sum - (self._tr_sum / p) + tr
            self._pdm_sum = self._pdm_sum - (self._pdm_sum / p) + pdm
            self._ndm_sum = self._ndm_sum - (self._ndm_sum / p) + ndm

        if self._tr_sum <= 0.0:
            dx = 0.0
        else:
            pdi = 100.0 * self._pdm_sum / self._tr_sum
            ndi = 100.0 * self._ndm_sum / self._tr_sum
            denom = pdi + ndi
            dx = (100.0 * abs(pdi - ndi) / denom) if denom > 0.0 else 0.0

        if self.value is None:
            self._dx_seed.append(dx)
            if len(self._dx_seed) == self.period:
                self.value = sum(self._dx_seed) / float(self.period)
                self._dx_seed.clear()
        else:
            self.value = ((self.value * float(self.period - 1)) + dx) / float(self.period)
        return self.value


class RollingEfficiencyRatio:
    """Коэффициент эффективности Кауфмана (ER = чистый ход / сумма шума за `period` баров)."""

    def __init__(self, period: int = 18):
        if period < 2:
            raise ValueError("EfficiencyRatio period must be >= 2")
        self.period: int = int(period)
        self._closes: Deque[float] = deque(maxlen=self.period + 1)
        self.value: Optional[float] = None

    def update(self, close: float) -> Optional[float]:
        self._closes.append(close)
        if len(self._closes) <= self.period:
            return None
        arr = list(self._closes)
        net_move = abs(arr[-1] - arr[0])
        noise_path = sum(abs(arr[i] - arr[i - 1]) for i in range(1, len(arr)))
        self.value = (net_move / noise_path) if noise_path > 0.0 else 0.0
        return self.value


def calc_atr_volatility_multiplier(
    atr: Optional[float],
    close: float,
    target_atr_pct_ratio: Optional[float],
    min_mult: float,
    max_mult: float,
) -> float:
    """Обратно пропорциональный сайзинг по ATR%: target / (ATR / Close) с клампом [min_mult, max_mult]."""
    if target_atr_pct_ratio is None or atr is None or atr <= 0.0 or close <= 0.0:
        return 1.0
    atr_pct = atr / close
    if atr_pct <= 0.0:
        return 1.0
    return max(min_mult, min(max_mult, target_atr_pct_ratio / atr_pct))


def calc_wide_atr_sl_extreme(
    side: str,
    close: float,
    atr: Optional[float],
    atr_sl_mult: Optional[float],
) -> Optional[float]:
    """Рассчитывает цену аварийного SL на расстоянии atr_sl_mult * ATR от текущей цены."""
    if atr is None or atr <= 0.0 or atr_sl_mult is None or atr_sl_mult <= 0.0:
        return None
    dist = atr * atr_sl_mult
    return max(1e-9, close - dist) if side == "LONG" else close + dist
