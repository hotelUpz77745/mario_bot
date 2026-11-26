# Path: MarioBot/CORE/STRATEGY/risk_sl_tp_calculator.py
# Role: Расчёт жёсткого Stop-Loss и Take-Profit (либо отключение SL/TP при sl_anchor='none')

from __future__ import annotations

from typing import Optional, Tuple
from MarioBot.CORE.config_loader import RiskManagementConfig
from MarioBot.CORE.models import SignalEvent
from MarioBot.CORE.spec_manager import SpecManager


class RiskStopTargetCalculator:
    """
    Рассчитывает уровни Stop-Loss и Take-Profit:
    - При sl_anchor='none' возвращает (None, None) (выход по обратному сигналу).
    - При sl_anchor='origin_swing_extreme' ставит жёсткий SL за экстремум свинга и TP по tp_rr_ratio.
    """

    def __init__(self, cfg: RiskManagementConfig, spec_manager: SpecManager):
        self.sl_anchor = cfg.sl_anchor
        self.sl_buffer_ratio = cfg.sl_buffer_ratio
        self.tp_rr_ratio = cfg.tp_rr_ratio
        self.spec_manager = spec_manager

    def calculate(self, signal: SignalEvent) -> Tuple[bool, Optional[float], Optional[float]]:
        """Возвращает (is_valid, sl_price, tp_price)."""
        if self.sl_anchor == "none":
            return True, None, None

        if signal.origin_extreme_price is None:
            return False, None, None

        entry = signal.entry_ref_price
        extreme = signal.origin_extreme_price
        buf = self.sl_buffer_ratio if self.sl_buffer_ratio is not None else 0.0

        if signal.side == "LONG":
            sl_price = self.spec_manager.round_price(signal.symbol, extreme * (1.0 - buf))
            risk_dist = entry - sl_price
            if risk_dist <= 0.0:
                return False, None, None
            tp_price: Optional[float] = None
            if self.tp_rr_ratio is not None:
                tp_price = self.spec_manager.round_price(signal.symbol, entry + (risk_dist * self.tp_rr_ratio))
                if tp_price <= entry:
                    return False, None, None
            return True, sl_price, tp_price

        sl_price = self.spec_manager.round_price(signal.symbol, extreme * (1.0 + buf))
        risk_dist = sl_price - entry
        if risk_dist <= 0.0:
            return False, None, None
        tp_price = None
        if self.tp_rr_ratio is not None:
            tp_price = self.spec_manager.round_price(signal.symbol, entry - (risk_dist * self.tp_rr_ratio))
            if tp_price <= 0.0 or tp_price >= entry:
                return False, None, None
        return True, sl_price, tp_price
