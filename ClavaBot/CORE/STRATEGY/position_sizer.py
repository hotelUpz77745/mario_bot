# Path: ClavaBot/CORE/STRATEGY/position_sizer.py
# Role: Расчёт объёма позиции от риска на сделку или фиксированного нотионала с учётом плеча и лота

from __future__ import annotations

from typing import Optional
from ClavaBot.CORE.config_loader import CapitalConfig
from ClavaBot.CORE.spec_manager import SpecManager


class PositionSizer:
    """
    Рассчитывает количество контрактов/монет для входа:
    - режим fixed_notional_usd: фиксированный объём в USD, ограниченный доступной маржой * плечо;
    - режим risk_per_trade_ratio: при наличии stop_loss_price — риск / дистанция до SL;
      при отсутствии стопа — доля капитала * плечо.
    """

    def __init__(self, cfg: CapitalConfig, spec_manager: SpecManager):
        self.leverage = cfg.leverage
        self.risk_per_trade_ratio = cfg.risk_per_trade_ratio
        self.fixed_notional_usd = cfg.fixed_notional_usd
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
        if equity_usd <= 0.0 or free_margin_usd <= 0.0 or entry_price <= 0.0 or size_multiplier <= 0.0:
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

        max_notional_by_margin = free_margin_usd * float(self.leverage)
        final_notional = min(target_notional, max_notional_by_margin)
        raw_qty = final_notional / entry_price
        rounded_qty = self.spec_manager.round_qty(symbol, raw_qty)

        if rounded_qty <= 0.0:
            return 0.0
        if not self.spec_manager.check_min_notional(symbol, rounded_qty, entry_price):
            return 0.0
        return rounded_qty
