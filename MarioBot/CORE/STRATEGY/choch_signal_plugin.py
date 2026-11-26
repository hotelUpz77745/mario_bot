# Path: MarioBot/CORE/STRATEGY/choch_signal_plugin.py
# Role: Сигнальный плагин 4H CHoCH (обёртка над SwingDetector и ChochBreakoutDeterminator)

from __future__ import annotations

from typing import Dict, Optional
from MarioBot.CORE.config_loader import (
    ChochDeterminatorConfig,
    StrategyPassport,
    SwingDetectorConfig,
)
from MarioBot.CORE.models import Candle, SignalEvent
from MarioBot.CORE.STRATEGY.base_signal_plugin import BaseSignalPlugin
from MarioBot.CORE.STRATEGY.choch_determinator import ChochBreakoutDeterminator
from MarioBot.CORE.STRATEGY.swing_detector import SwingDetector


class ChochSignalPlugin(BaseSignalPlugin):
    """Изолированный сигнальный плагин пробоя структуры CHoCH для мультисимвольного ядра."""

    def __init__(self, passport: StrategyPassport):
        params = passport.signal.params
        swing_cfg = SwingDetectorConfig(
            left_candles=int(params["left_candles"]),
            right_candles=int(params["right_candles"]),
            min_swing_sequence=int(params["min_swing_sequence"]),
        )
        choch_cfg = ChochDeterminatorConfig(
            breakout_price_source=str(params["breakout_price_source"]),
            require_sweep=bool(params["require_sweep"]),
            entry_order_type=str(params["entry_order_type"]),
        )
        if swing_cfg.left_candles < 1 or swing_cfg.right_candles < 1:
            raise ValueError("left_candles and right_candles must be >= 1")
        if choch_cfg.breakout_price_source != "close":
            raise ValueError("breakout_price_source must be 'close'")
        if choch_cfg.require_sweep:
            raise ValueError("require_sweep must be false per strategy specification")

        self.swing_detectors: Dict[str, SwingDetector] = {
            sym: SwingDetector(sym, swing_cfg)
            for sym in passport.symbols
        }
        self.determinator = ChochBreakoutDeterminator(choch_cfg)

    def update_and_evaluate(self, symbol: str, candle: Candle) -> Optional[SignalEvent]:
        structure = self.swing_detectors[symbol].update(candle)
        return self.determinator.evaluate(candle, structure)
