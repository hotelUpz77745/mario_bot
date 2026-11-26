# Path: MarioBot/CORE/STRATEGY/position_sizer.py
# Role: Расчёт объёма позиции для режимов reinvest / fixed (без участия плеча в формулах)

from __future__ import annotations

from typing import Optional
from MarioBot.CORE.config_loader import CapitalConfig
from MarioBot.CORE.spec_manager import SpecManager


class PositionSizer:
    """
    Рассчитывает количество контрактов/монет для входа БЕЗ участия плеча в формулах:
    - mode == 'reinvest': target_notional = equity_usd * notional_equity_ratio * (mult if apply_signal_multipliers else 1.0);
    - mode == 'fixed': target_notional = fixed_notional_usd * (mult if apply_signal_multipliers else 1.0);
    - mode == 'legacy_risk' (обратная совместимость для архивных бэктест-паспортов из CFG/artifacts/).
    """

    def __init__(self, cfg: CapitalConfig, spec_manager: SpecManager):
        self.sizing_mode = cfg.sizing_mode
        self.leverage = cfg.leverage
        self.reinvest_notional_equity_ratio = cfg.reinvest_notional_equity_ratio
        self.reinvest_apply_signal_multipliers = cfg.reinvest_apply_signal_multipliers
        self.fixed_notional_usd = cfg.fixed_notional_usd
        self.fixed_apply_signal_multipliers = cfg.fixed_apply_signal_multipliers
        self.risk_per_trade_ratio = cfg.risk_per_trade_ratio
        self.spec_manager = spec_manager

    def calculate_qty(
        self,
        symbol: str,
        equity_usd: float,
        free_margin_usd: float,
        entry_price: float,
        stop_loss_price: Optional[float],
        size_multiplier: float = 1.0,
    ) -> float:
        if equity_usd <= 0.0 or entry_price <= 0.0 or size_multiplier <= 0.0:
            return 0.0

        if self.sizing_mode == "reinvest":
            if self.reinvest_notional_equity_ratio is None or self.reinvest_notional_equity_ratio <= 0.0:
                return 0.0
            mult = size_multiplier if self.reinvest_apply_signal_multipliers else 1.0
            final_notional = equity_usd * self.reinvest_notional_equity_ratio * mult
        elif self.sizing_mode == "fixed":
            if self.fixed_notional_usd is None or self.fixed_notional_usd <= 0.0:
                return 0.0
            mult = size_multiplier if self.fixed_apply_signal_multipliers else 1.0
            final_notional = self.fixed_notional_usd * mult
        else:
            # Архивный режим бэктеста (CFG/artifacts/*)
            if free_margin_usd <= 0.0:
                return 0.0
            if self.fixed_notional_usd is not None:
                target_notional = self.fixed_notional_usd * size_multiplier
            elif self.risk_per_trade_ratio is not None:
                if stop_loss_price is not None:
                    sl_distance = abs(entry_price - stop_loss_price)
                    if sl_distance <= 0.0:
                        return 0.0
                    risk_usd = equity_usd * self.risk_per_trade_ratio * size_multiplier
                    target_notional = (risk_usd / sl_distance) * entry_price
                else:
                    target_notional = equity_usd * self.risk_per_trade_ratio * float(self.leverage) * size_multiplier
            else:
                return 0.0
            final_notional = min(target_notional, free_margin_usd * float(self.leverage))

        raw_qty = final_notional / entry_price
        rounded_qty = self.spec_manager.round_qty(symbol, raw_qty)

        if rounded_qty <= 0.0:
            return 0.0
        if not self.spec_manager.check_min_notional(symbol, rounded_qty, entry_price):
            return 0.0
        return rounded_qty
