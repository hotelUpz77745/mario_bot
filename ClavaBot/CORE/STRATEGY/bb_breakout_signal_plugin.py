# Path: ClavaBot/CORE/STRATEGY/bb_breakout_signal_plugin.py
# Role: Сигнальный плагин пробоя полос Боллинджера (Trading Rush из notes.md)

from __future__ import annotations

from collections import deque
from typing import Deque, Dict, Optional
from ClavaBot.CORE.config_loader import StrategyPassport
from ClavaBot.CORE.models import Candle, SignalEvent
from ClavaBot.CORE.STRATEGY._indicators import RollingBollinger
from ClavaBot.CORE.STRATEGY.base_signal_plugin import BaseSignalPlugin


class _SymbolBBState:
    __slots__ = ("bb", "highs", "lows", "prev_close", "prev_upper", "prev_lower")

    def __init__(self, bb_period: int, bb_std_mult: float, swing_lookback: int):
        self.bb = RollingBollinger(bb_period, bb_std_mult)
        self.highs: Deque[float] = deque(maxlen=swing_lookback)
        self.lows: Deque[float] = deque(maxlen=swing_lookback)
        self.prev_close: Optional[float] = None
        self.prev_upper: Optional[float] = None
        self.prev_lower: Optional[float] = None


class BollingerBreakoutSignalPlugin(BaseSignalPlugin):
    """
    Стратегия #1 из notes.md (Trading Rush):
    - Вход LONG на первой свече, закрывшейся выше верхней полосы Боллинджера;
    - Вход SHORT на первой свече, закрывшейся ниже нижней полосы Боллинджера;
    - Стоп-экстремум: минимум/максимум за последние swing_lookback_candles свечей;
    - Тренд-фильтр EMA(200) подключается через EmaTrendFilter в оркестраторе.
    """

    def __init__(self, passport: StrategyPassport):
        params = passport.signal.params
        self.bb_period: int = int(params["bb_period"])
        self.bb_std_mult: float = float(params["bb_std_mult"])
        self.swing_lookback: int = int(params["swing_lookback_candles"])
        if self.swing_lookback < 2:
            raise ValueError("swing_lookback_candles must be >= 2")

        self._states: Dict[str, _SymbolBBState] = {
            sym: _SymbolBBState(self.bb_period, self.bb_std_mult, self.swing_lookback)
            for sym in passport.symbols
        }

    def update_and_evaluate(self, symbol: str, candle: Candle) -> Optional[SignalEvent]:
        st = self._states[symbol]
        st.highs.append(candle.high)
        st.lows.append(candle.low)
        bands = st.bb.update(candle.close)

        if bands is None or st.prev_close is None or st.prev_upper is None or st.prev_lower is None:
            if bands is not None:
                st.prev_upper, _mid, st.prev_lower = bands
            st.prev_close = candle.close
            return None

        upper, _mid, lower = bands
        sig: Optional[SignalEvent] = None

        if len(st.lows) >= self.swing_lookback:
            if st.prev_close <= st.prev_upper and candle.close > upper:
                sig = SignalEvent(
                    symbol=symbol,
                    side="LONG",
                    signal_ts_ms=candle.close_ts_ms,
                    entry_ref_price=candle.close,
                    broken_level_price=upper,
                    origin_extreme_price=min(st.lows),
                )
            elif st.prev_close >= st.prev_lower and candle.close < lower:
                sig = SignalEvent(
                    symbol=symbol,
                    side="SHORT",
                    signal_ts_ms=candle.close_ts_ms,
                    entry_ref_price=candle.close,
                    broken_level_price=lower,
                    origin_extreme_price=max(st.highs),
                )

        st.prev_close = candle.close
        st.prev_upper = upper
        st.prev_lower = lower
        return sig
