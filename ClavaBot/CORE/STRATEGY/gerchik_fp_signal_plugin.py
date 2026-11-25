# Path: ClavaBot/CORE/STRATEGY/gerchik_fp_signal_plugin.py
# Role: Сигнальный плагин Александра Герчика (ложный пробой уровня двумя барами)

from __future__ import annotations

from collections import deque
from typing import Deque, Dict, Optional
from ClavaBot.CORE.config_loader import StrategyPassport
from ClavaBot.CORE.models import Candle, SignalEvent
from ClavaBot.CORE.STRATEGY._indicators import IncrementalATR
from ClavaBot.CORE.STRATEGY.base_signal_plugin import BaseSignalPlugin


class _GerchikState:
    __slots__ = ("atr", "bars")

    def __init__(self, atr_p: int, level_lb: int):
        self.atr = IncrementalATR(atr_p)
        self.bars: Deque[Candle] = deque(maxlen=level_lb + 2)


class GerchikTwoBarFpSignalPlugin(BaseSignalPlugin):
    """
    Стратегия Александра Герчика из notes.md (ложный пробой двумя барами):
    - Уровень строится по max(high)/min(low) за предыдущие level_lookback_candles до пред-предыдущего бара;
    - SHORT: bar[t-1] закрылся ВЫШЕ level_high, bar[t] закрылся обратно НИЖЕ level_high;
    - LONG: bar[t-1] закрылся НИЖЕ level_low, bar[t] закрылся обратно ВЫШЕ level_low;
    - Стоп: за экстремум двухбарового пробоя + atr_buffer_mult * ATR(14).
    """

    def __init__(self, passport: StrategyPassport):
        params = passport.signal.params
        self.level_lb = int(params["level_lookback_candles"])
        self.atr_p = int(params["atr_period"])
        self.atr_buf = float(params["atr_sl_buffer_mult"])
        self._states: Dict[str, _GerchikState] = {
            sym: _GerchikState(self.atr_p, self.level_lb)
            for sym in passport.symbols
        }

    def update_and_evaluate(self, symbol: str, candle: Candle) -> Optional[SignalEvent]:
        st = self._states[symbol]
        atr = st.atr.update(candle)
        st.bars.append(candle)

        if atr is None or atr <= 0.0 or len(st.bars) < self.level_lb + 2:
            return None

        history = list(st.bars)[:-2]
        b_prev, b_cur = st.bars[-2], st.bars[-1]
        res_lvl = max(b.high for b in history)
        sup_lvl = min(b.low for b in history)

        if b_prev.close < sup_lvl and b_cur.close > sup_lvl:
            sl_ext = min(b_prev.low, b_cur.low) - (self.atr_buf * atr)
            return SignalEvent(
                symbol=symbol,
                side="LONG",
                signal_ts_ms=b_cur.close_ts_ms,
                entry_ref_price=b_cur.close,
                broken_level_price=sup_lvl,
                origin_extreme_price=max(1e-9, sl_ext),
            )

        if b_prev.close > res_lvl and b_cur.close < res_lvl:
            sl_ext = max(b_prev.high, b_cur.high) + (self.atr_buf * atr)
            return SignalEvent(
                symbol=symbol,
                side="SHORT",
                signal_ts_ms=b_cur.close_ts_ms,
                entry_ref_price=b_cur.close,
                broken_level_price=res_lvl,
                origin_extreme_price=sl_ext,
            )
        return None
