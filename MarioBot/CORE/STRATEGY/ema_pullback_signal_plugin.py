# Path: MarioBot/CORE/STRATEGY/ema_pullback_signal_plugin.py
# Role: Сигнальный плагин отката к зоне EMA 20-50 со стопом 2 ATR и трейлингом по EMA (Rayner Teo)

from __future__ import annotations

from typing import Dict, Optional
from MarioBot.CORE.config_loader import StrategyPassport
from MarioBot.CORE.models import Candle, ExitReasonType, OpenPosition, SignalEvent
from MarioBot.CORE.STRATEGY._indicators import IncrementalATR, IncrementalEMA
from MarioBot.CORE.STRATEGY.base_signal_plugin import BaseSignalPlugin


class _PullbackState:
    __slots__ = ("ema_fast", "ema_slow", "ema_trail", "atr", "prev_candle", "touch_long_age", "touch_short_age")

    def __init__(self, fast_p: int, slow_p: int, trail_p: int, atr_p: int):
        self.ema_fast = IncrementalEMA(fast_p)
        self.ema_slow = IncrementalEMA(slow_p)
        self.ema_trail = IncrementalEMA(trail_p)
        self.atr = IncrementalATR(atr_p)
        self.prev_candle: Optional[Candle] = None
        self.touch_long_age: int = 999
        self.touch_short_age: int = 999


class EmaPullbackSignalPlugin(BaseSignalPlugin):
    """
    Стратегия #3 из notes.md (Rayner Teo Pullback + 2 ATR Stop + Trailing EMA):
    - LONG: EMA20 > EMA50, недавний откат в зону EMA20..50 и закрытие бычьей свечи выше prev_high;
    - SHORT: EMA20 < EMA50, недавний откат в зону EMA20..50 и закрытие медвежьей свечи ниже prev_low;
    - SL = 2.0 * ATR(14), фиксированный TP отсутствует, выход при пробое скользящей ema_trail.
    """

    def __init__(self, passport: StrategyPassport):
        params = passport.signal.params
        self.fast_period = int(params["fast_ema_period"])
        self.slow_period = int(params["slow_ema_period"])
        self.trail_period = int(params["trail_ema_period"])
        self.atr_period = int(params["atr_period"])
        self.atr_sl_mult = float(params["atr_sl_mult"])
        self.pullback_lookback = int(params["pullback_lookback_candles"])

        self._states: Dict[str, _PullbackState] = {
            sym: _PullbackState(self.fast_period, self.slow_period, self.trail_period, self.atr_period)
            for sym in passport.symbols
        }

    def update_and_evaluate(self, symbol: str, candle: Candle) -> Optional[SignalEvent]:
        st = self._states[symbol]
        fast = st.ema_fast.update(candle.close)
        slow = st.ema_slow.update(candle.close)
        st.ema_trail.update(candle.close)
        atr = st.atr.update(candle)
        prev = st.prev_candle
        st.prev_candle = candle

        st.touch_long_age += 1
        st.touch_short_age += 1

        if fast is None or slow is None or atr is None or prev is None or atr <= 0.0:
            return None

        sig: Optional[SignalEvent] = None
        if fast > slow:
            if candle.low <= fast:
                st.touch_long_age = 0
            if st.touch_long_age <= self.pullback_lookback and candle.close > candle.open and candle.close > prev.high:
                sig = SignalEvent(
                    symbol=symbol,
                    side="LONG",
                    signal_ts_ms=candle.close_ts_ms,
                    entry_ref_price=candle.close,
                    origin_extreme_price=max(1e-9, candle.close - (self.atr_sl_mult * atr)),
                )
        elif fast < slow:
            if candle.high >= fast:
                st.touch_short_age = 0
            if st.touch_short_age <= self.pullback_lookback and candle.close < candle.open and candle.close < prev.low:
                sig = SignalEvent(
                    symbol=symbol,
                    side="SHORT",
                    signal_ts_ms=candle.close_ts_ms,
                    entry_ref_price=candle.close,
                    origin_extreme_price=candle.close + (self.atr_sl_mult * atr),
                )

        return sig

    def check_exit(self, symbol: str, candle: Candle, open_pos: OpenPosition) -> Optional[ExitReasonType]:
        trail = self._states[symbol].ema_trail.value
        if trail is None:
            return None
        if open_pos.side == "LONG" and candle.close < trail:
            return "SIGNAL_EXIT"
        if open_pos.side == "SHORT" and candle.close > trail:
            return "SIGNAL_EXIT"
        return None
