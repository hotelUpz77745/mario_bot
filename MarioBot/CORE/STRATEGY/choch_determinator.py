# Path: MarioBot/CORE/STRATEGY/choch_determinator.py
# Role: Детерминатор пробоя структуры (CHoCH) строго по закрытию тела свечи за HL/LH

from __future__ import annotations

from typing import Dict, Optional, Tuple
from MarioBot.CORE.config_loader import ChochDeterminatorConfig
from MarioBot.CORE.models import Candle, MarketStructureState, SideType, SignalEvent


class ChochBreakoutDeterminator:
    """
    Определяет первый подтверждённый пробой структуры (CHoCH):
    - LONG: при BEARISH-структуре закрытие тела свечи выше последнего LH.
    - SHORT: при BULLISH-структуре закрытие тела свечи ниже последнего HL.
    Защита от повторного срабатывания на одном и том же уровне (side, broken_bar_index).
    """

    def __init__(self, cfg: ChochDeterminatorConfig):
        self.breakout_price_source = cfg.breakout_price_source
        self.require_sweep = cfg.require_sweep
        self.entry_order_type = cfg.entry_order_type
        self._consumed_levels: Dict[str, Tuple[SideType, int]] = {}

    def evaluate(self, candle: Candle, structure: MarketStructureState) -> Optional[SignalEvent]:
        if structure.regime == "BEARISH" and structure.last_lh is not None:
            level = structure.last_lh
            origin = structure.last_swing_low or structure.last_ll
            if origin is not None and candle.close > level.price:
                key = ("LONG", level.bar_index)
                if self._consumed_levels.get(candle.symbol) != key:
                    self._consumed_levels[candle.symbol] = key
                    return SignalEvent(
                        symbol=candle.symbol,
                        side="LONG",
                        signal_ts_ms=candle.close_ts_ms,
                        entry_ref_price=candle.close,
                        broken_level_price=level.price,
                        broken_level_bar_index=level.bar_index,
                        origin_extreme_price=origin.price,
                    )

        if structure.regime == "BULLISH" and structure.last_hl is not None:
            level = structure.last_hl
            origin = structure.last_swing_high or structure.last_hh
            if origin is not None and candle.close < level.price:
                key = ("SHORT", level.bar_index)
                if self._consumed_levels.get(candle.symbol) != key:
                    self._consumed_levels[candle.symbol] = key
                    return SignalEvent(
                        symbol=candle.symbol,
                        side="SHORT",
                        signal_ts_ms=candle.close_ts_ms,
                        entry_ref_price=candle.close,
                        broken_level_price=level.price,
                        broken_level_bar_index=level.bar_index,
                        origin_extreme_price=origin.price,
                    )

        return None
