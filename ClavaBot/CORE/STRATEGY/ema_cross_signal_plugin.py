# Path: ClavaBot/CORE/STRATEGY/ema_cross_signal_plugin.py
# Role: Инъектируемый сигнальный плагин боевого EMA-кроссовера с фильтром ускорения и импульса

from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, List, Optional
from ClavaBot.CORE.config_loader import StrategyPassport
from ClavaBot.CORE.models import Candle, OpenPosition, SideType, SignalEvent
from ClavaBot.CORE.STRATEGY._indicators import (
    IncrementalADX,
    IncrementalATR,
    calc_atr_volatility_multiplier,
    calc_wide_atr_sl_extreme,
)
from ClavaBot.CORE.STRATEGY.base_signal_plugin import BaseSignalPlugin
from ClavaBot.CORE.STRATEGY.markov_regime_filter import MarkovRegimeFilter


class IndicatorsMath:
    """Математика расчёта экспоненциальной скользящей средней (EMA с SMA-инициализацией)."""

    @staticmethod
    def calc_ema(closes: List[float], period: int) -> List[float]:
        if period <= 0 or len(closes) < period:
            return []
        sma_seed = sum(closes[:period]) / float(period)
        ema_values: List[float] = [sma_seed]
        alpha = 2.0 / float(period + 1)
        one_minus = 1.0 - alpha
        prev = sma_seed
        for px in closes[period:]:
            cur = (px * alpha) + (prev * one_minus)
            ema_values.append(cur)
            prev = cur
        return ema_values


class EMACrossCalculator:
    """
    Изолированный калькулятор боевого EMA-кроссовера с фильтром ускорения и импульса.
    Конфигурация читается строго через ['key'] без .get(..., default).
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.is_active: bool = bool(cfg["is_active"])
        self.timeframe: str = str(cfg["timeframe"])
        self.period1: int = int(cfg["period1"])
        self.period2: int = int(cfg["period2"])
        self.long_cond: str = str(cfg["long_cond"])
        self.short_cond: str = str(cfg["short_cond"])

    def calculate(self, closes: List[float]) -> List[str]:
        """
        Вычисляет состояние EMA-кроссовера с фильтром ускорения и импульса:
        - CROSS_UP: быстрая EMA пересекает медленную снизу вверх с нарастающим расхождением (импульс)
        - CROSS_DOWN: быстрая EMA пересекает медленную сверху вниз с нарастающим расхождением (импульс)
        - UNSTABLE: недостаточно свечей
        - []: нет сигнала
        """
        if not self.is_active or not closes:
            return ["UNSTABLE"]

        req_len = max(self.period1, self.period2) + 5
        if len(closes) < req_len:
            return ["UNSTABLE"]

        ema1 = IndicatorsMath.calc_ema(closes, self.period1)
        ema2 = IndicatorsMath.calc_ema(closes, self.period2)

        min_len = min(len(ema1), len(ema2))
        if min_len < 3:
            return ["UNSTABLE"]

        e1_tail = ema1[-min_len:]
        e2_tail = ema2[-min_len:]

        diff = [e1 - e2 for e1, e2 in zip(e1_tail, e2_tail)]
        if len(diff) < 3:
            return ["UNSTABLE"]

        d_now = diff[-1]
        d_prev = diff[-2]
        d_prev2 = diff[-3]

        delta_now = abs(d_now - d_prev)
        delta_prev = abs(d_prev - d_prev2)
        impulse = delta_now > delta_prev

        cross_up = (d_now > 0.0) and (d_prev2 < 0.0) and (d_prev > d_prev2) and impulse
        cross_down = (d_now < 0.0) and (d_prev2 > 0.0) and (d_prev < d_prev2) and impulse

        signals: List[str] = []
        if cross_up:
            signals.append("CROSS_UP")
        if cross_down:
            signals.append("CROSS_DOWN")
        return signals


class EmaCrossSignalPlugin(BaseSignalPlugin):
    """Адаптер EMACrossCalculator к универсальному интерфейсу BaseSignalPlugin."""

    def __init__(self, passport: StrategyPassport):
        params = dict(passport.signal.params)
        calc_cfg = {
            "is_active": bool(params["is_active"]),
            "timeframe": passport.timeframe,
            "period1": int(params["period1"]),
            "period2": int(params["period2"]),
            "long_cond": str(params["long_cond"]),
            "short_cond": str(params["short_cond"]),
        }
        self.calculator = EMACrossCalculator(calc_cfg)
        self.adx_period: int = int(params["adx_period"])
        self.min_adx: Optional[float] = float(params["min_adx"]) if params["min_adx"] is not None else None
        self.probe_in_sideways_ratio: Optional[float] = (
            float(params["probe_in_sideways_ratio"]) if params["probe_in_sideways_ratio"] is not None else None
        )
        self.probe_adx_threshold: Optional[float] = (
            float(params["probe_adx_threshold"]) if params["probe_adx_threshold"] is not None else None
        )
        self.scale_up_adx_min: Optional[float] = (
            float(params["scale_up_adx_min"]) if params["scale_up_adx_min"] is not None else None
        )
        self.atr_period: int = int(params["atr_period"])
        self.target_atr_pct_ratio: Optional[float] = (
            float(params["target_atr_pct_ratio"]) if params["target_atr_pct_ratio"] is not None else None
        )
        self.vol_min_multiplier: float = float(params["vol_min_multiplier"])
        self.vol_max_multiplier: float = float(params["vol_max_multiplier"])
        self.sl_mode: str = str(params["sl_mode"])
        self.atr_sl_mult: Optional[float] = (
            float(params["atr_sl_mult"]) if params["atr_sl_mult"] is not None else None
        )

        buf_len = max(200, max(self.calculator.period1, self.calculator.period2) * 8)
        self._closes: Dict[str, Deque[float]] = {sym: deque(maxlen=buf_len) for sym in passport.symbols}
        self._adxs: Dict[str, IncrementalADX] = {sym: IncrementalADX(self.adx_period) for sym in passport.symbols}
        self._atrs: Dict[str, IncrementalATR] = {sym: IncrementalATR(self.atr_period) for sym in passport.symbols}
        self._mk_tracker: Optional[MarkovRegimeFilter] = (
            MarkovRegimeFilter(passport.markov_regime)
            if (self.probe_in_sideways_ratio is not None and passport.markov_regime.enabled)
            else None
        )
        self._last_day_idx: Dict[str, int] = {}
        self._cur_mk_state: Dict[str, str] = {}
        self._waiting_scale_up: Dict[str, bool] = {}

    def update_and_evaluate(self, symbol: str, candle: Candle) -> Optional[SignalEvent]:
        adx = self._adxs[symbol].update(candle)
        atr = self._atrs[symbol].update(candle)
        if self._mk_tracker is not None:
            self._mk_tracker.update(symbol, candle)
            day_idx = (candle.close_ts_ms - 1) // 86_400_000
            if self._last_day_idx.get(symbol) != day_idx:
                self._last_day_idx[symbol] = day_idx
                self._cur_mk_state[symbol] = self._mk_tracker.evaluate(symbol, "LONG").current_state

        buf = self._closes[symbol]
        buf.append(candle.close)
        signals = self.calculator.calculate(list(buf))
        if not signals or signals[0] == "UNSTABLE":
            return None

        side: Optional[SideType] = None
        if self.calculator.long_cond in signals:
            side = "LONG"
        elif self.calculator.short_cond in signals:
            side = "SHORT"
        if side is None:
            return None

        if self.min_adx is not None and adx is not None and adx < self.min_adx:
            return None

        origin_extreme: Optional[float] = None
        if self.sl_mode == "wide_atr":
            origin_extreme = calc_wide_atr_sl_extreme(side, candle.close, atr, self.atr_sl_mult)
            if origin_extreme is None:
                return None

        size_mult = 1.0
        if self.probe_in_sideways_ratio is not None:
            in_sideways = self._cur_mk_state.get(symbol, "WARMING") == "SIDEWAYS"
            weak_adx = self.probe_adx_threshold is not None and adx is not None and adx < self.probe_adx_threshold
            if in_sideways or weak_adx:
                size_mult = self.probe_in_sideways_ratio
                self._waiting_scale_up[symbol] = True
            else:
                self._waiting_scale_up[symbol] = False

        size_mult *= calc_atr_volatility_multiplier(
            atr,
            candle.close,
            self.target_atr_pct_ratio,
            self.vol_min_multiplier,
            self.vol_max_multiplier,
        )

        return SignalEvent(
            symbol=symbol,
            side=side,
            signal_ts_ms=candle.close_ts_ms,
            entry_ref_price=candle.close,
            origin_extreme_price=origin_extreme,
            size_multiplier=size_mult,
        )

    def check_scale_in(self, symbol: str, candle: Candle, open_pos: OpenPosition) -> Optional[float]:
        if self.probe_in_sideways_ratio is None or not self._waiting_scale_up.get(symbol, False):
            return None
        in_profit = (candle.close > open_pos.entry_price) if open_pos.side == "LONG" else (candle.close < open_pos.entry_price)
        if not in_profit:
            return None
        if self._mk_tracker is not None and self._cur_mk_state.get(symbol, "WARMING") == "SIDEWAYS":
            return None
        adx = self._adxs[symbol].value
        if self.scale_up_adx_min is not None and (adx is None or adx < self.scale_up_adx_min):
            return None
        self._waiting_scale_up[symbol] = False
        return calc_atr_volatility_multiplier(
            self._atrs[symbol].value,
            candle.close,
            self.target_atr_pct_ratio,
            self.vol_min_multiplier,
            self.vol_max_multiplier,
        )
