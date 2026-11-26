# Path: MarioBot/CORE/STRATEGY/remora_dynamic_sizer.py
# Role: Динамический сайзер на стороне бота Маркова для концентрированного распределения маржи
#       между реально сработавшими прилипалами и перенесёнными открытыми позициями.

from __future__ import annotations

from MarioBot.CORE.config_loader import RemoraDynamicSizingConfig
from MarioBot.CORE.spec_manager import SpecManager


class RemoraDynamicSizer:
    """
    Распределяет доступный капитал Mario динамически:
    - Не режет депозит фиксированно на 1/10 (как при полной корзине), если в просадке 1-3 монеты.
    - Делит equity * equity_utilization на effective_slots = clamp(open + triggered, min_slots, max_slots),
      ограничивает базу коридором [min_slot_equity_pct, max_slot_equity_pct] от текущего equity,
      применяет size_multiplier (Марков + ATR% + Sentinel) и защитный лимит свободной маржи.
    """

    def __init__(self, leverage: int, cfg: RemoraDynamicSizingConfig, spec_manager: SpecManager):
        self.leverage = int(leverage)
        self.cfg = cfg
        self.spec_manager = spec_manager

    def compute_base_notional(
        self,
        equity_usd: float,
        open_positions_count: int,
        triggered_pool_count: int,
    ) -> float:
        if equity_usd <= 0.0:
            return 0.0
        demand_slots = max(0, open_positions_count) + max(1, triggered_pool_count)
        effective_slots = max(self.cfg.min_slots, min(self.cfg.max_slots, demand_slots))
        raw_base = (equity_usd * self.cfg.equity_utilization) / float(effective_slots)
        min_base = equity_usd * self.cfg.min_slot_equity_pct
        max_base = equity_usd * self.cfg.max_slot_equity_pct
        return max(min_base, min(max_base, raw_base))

    def calculate_qty(
        self,
        symbol: str,
        equity_usd: float,
        free_margin_usd: float,
        entry_price: float,
        open_positions_count: int,
        triggered_pool_count: int,
        size_multiplier: float = 1.0,
    ) -> float:
        if equity_usd <= 0.0 or free_margin_usd <= 0.0 or entry_price <= 0.0 or size_multiplier <= 0.0:
            return 0.0

        base_notional = self.compute_base_notional(
            equity_usd=equity_usd,
            open_positions_count=open_positions_count,
            triggered_pool_count=triggered_pool_count,
        )
        if base_notional <= 0.0:
            return 0.0

        target_notional = base_notional * size_multiplier
        max_notional_by_margin = free_margin_usd * float(self.leverage) * self.cfg.margin_safety_ratio
        final_notional = min(target_notional, max_notional_by_margin)
        if final_notional <= 0.0:
            return 0.0

        raw_qty = final_notional / entry_price
        rounded_qty = self.spec_manager.round_qty(symbol, raw_qty)
        if rounded_qty <= 0.0:
            return 0.0
        if not self.spec_manager.check_min_notional(symbol, rounded_qty, entry_price):
            return 0.0
        return rounded_qty
