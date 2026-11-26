# Path: MarioBot/CORE/STRATEGY/macd_anti_signal_plugin.py
# Role: Сигнальный плагин SMB Capital / Рашке MACD (3, 10, 16) паттерн «Anti»

from __future__ import annotations

from collections import deque
from typing import Deque, Dict, Optional
from MarioBot.CORE.config_loader import StrategyPassport
from MarioBot.CORE.models import Candle, SignalEvent
from MarioBot.CORE.STRATEGY._indicators import IncrementalMACD
from MarioBot.CORE.STRATEGY.base_signal_plugin import BaseSignalPlugin


class _AntiState:
    __slots__ = ("macd", "prev_macd", "prev_sig", "highs", "lows")

    def __init__(self, swing_lb: int):
        self.macd = IncrementalMACD(3, 10, 16)
        self.prev_macd: Optional[float] = None
        self.prev_sig: Optional[float] = None
        self.highs: Deque[float] = deque(maxlen=swing_lb)
        self.lows: Deque[float] = deque(maxlen=swing_lb)


class MacdAntiSignalPlugin(BaseSignalPlugin):
    """
    Стратегия SMB Capital / Линды Рашке из notes.md (MACD 3-10-16 «Anti»):
    - LONG: сигнальная линия > 0, быстрый MACD откатывался под сигнальную и пробивает её обратно вверх;
    - SHORT: сигнальная линия < 0, быстрый MACD откатывался над сигнальной и пробивает её обратно вниз;
    - Стоп за локальный экстремум отката.
    """

    def __init__(self, passport: StrategyPassport):
        params = passport.signal.params
        self.swing_lb = int(params["swing_lookback_candles"])
        self._states: Dict[str, _AntiState] = {
            sym: _AntiState(self.swing_lb)
            for sym in passport.symbols
        }

    def update_and_evaluate(self, symbol: str, candle: Candle) -> Optional[SignalEvent]:
        st = self._states[symbol]
        st.highs.append(candle.high)
        st.lows.append(candle.low)
        res = st.macd.update(candle.close)
        if res is None:
            return None

        cur_m, cur_s = res
        prev_m, prev_s = st.prev_macd, st.prev_sig
        st.prev_macd, st.prev_sig = cur_m, cur_s

        if prev_m is None or prev_s is None or len(st.lows) < self.swing_lb:
            return None

        if cur_s > 0.0 and prev_m <= prev_s and cur_m > cur_s:
            return SignalEvent(
                symbol=symbol,
                side="LONG",
                signal_ts_ms=candle.close_ts_ms,
                entry_ref_price=candle.close,
                origin_extreme_price=min(st.lows),
            )
        if cur_s < 0.0 and prev_m >= prev_s and cur_m < cur_s:
            return SignalEvent(
                symbol=symbol,
                side="SHORT",
                signal_ts_ms=candle.close_ts_ms,
                entry_ref_price=candle.close,
                origin_extreme_price=max(st.highs),
            )
        return None
