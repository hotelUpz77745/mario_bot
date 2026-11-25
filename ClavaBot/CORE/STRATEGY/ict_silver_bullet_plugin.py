# Path: ClavaBot/CORE/STRATEGY/ict_silver_bullet_plugin.py
# Role: Сигнальный плагин ICT Silver Bullet (снятие ликвидности + 3-свечный имбаланс FVG)

from __future__ import annotations

from collections import deque
from typing import Deque, Dict, Optional
from ClavaBot.CORE.config_loader import StrategyPassport
from ClavaBot.CORE.models import Candle, SignalEvent
from ClavaBot.CORE.STRATEGY.base_signal_plugin import BaseSignalPlugin


class IctSilverBulletSignalPlugin(BaseSignalPlugin):
    """
    Стратегия ICT Silver Bullet из notes.md:
    - Бычий FVG (b2.low > b0.high) после снятия sell-side ликвидности (min low предыдущего окна);
    - Медвежий FVG (b2.high < b0.low) после снятия buy-side ликвидности (max high предыдущего окна);
    - Сессионное окно NY задаётся через SessionWindowFilter паспорта.
    """

    def __init__(self, passport: StrategyPassport):
        params = passport.signal.params
        self.sweep_lb = int(params["sweep_lookback_candles"])
        if self.sweep_lb < 4:
            raise ValueError("sweep_lookback_candles must be >= 4")
        self._bars: Dict[str, Deque[Candle]] = {
            sym: deque(maxlen=self.sweep_lb + 3)
            for sym in passport.symbols
        }

    def update_and_evaluate(self, symbol: str, candle: Candle) -> Optional[SignalEvent]:
        buf = self._bars[symbol]
        buf.append(candle)
        if len(buf) < self.sweep_lb + 3:
            return None

        b0, b1, b2 = buf[-3], buf[-2], buf[-1]
        prior = list(buf)[:-3]
        prior_low = min(b.low for b in prior)
        prior_high = max(b.high for b in prior)

        bull_sweep = min(b0.low, b1.low, b2.low) < prior_low
        if bull_sweep and b2.low > b0.high and b2.close > b1.high:
            return SignalEvent(
                symbol=symbol,
                side="LONG",
                signal_ts_ms=b2.close_ts_ms,
                entry_ref_price=b2.close,
                broken_level_price=prior_low,
                origin_extreme_price=min(b0.low, b1.low),
            )

        bear_sweep = max(b0.high, b1.high, b2.high) > prior_high
        if bear_sweep and b2.high < b0.low and b2.close < b1.low:
            return SignalEvent(
                symbol=symbol,
                side="SHORT",
                signal_ts_ms=b2.close_ts_ms,
                entry_ref_price=b2.close,
                broken_level_price=prior_high,
                origin_extreme_price=max(b0.high, b1.high),
            )
        return None
