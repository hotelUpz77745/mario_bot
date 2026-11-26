# Path: MarioBot/CORE/EXECUTION/mock_live_broker.py
# Role: Мок-адаптер боевого брокера для сухого запуска MarioBot без реальных ордеров

from __future__ import annotations

from typing import Dict, Optional
from MarioBot.c_log import UnifiedLogger
from MarioBot.CORE.EXECUTION.base_broker import ExecutionBroker
from MarioBot.CORE.models import OpenPosition, OrderPlan, SideType


class MockLiveExecutionBroker(ExecutionBroker):
    """Имитирует исполнение рыночных ордеров и хранение открытых позиций."""

    def __init__(self, start_balance_usd: float, leverage: int, taker_fee_rate: float):
        self.logger = UnifiedLogger("MockLiveBroker")
        self.balance_usd: float = float(start_balance_usd)
        self.leverage: int = int(leverage)
        self.taker_fee_rate: float = float(taker_fee_rate)
        self.positions: Dict[str, OpenPosition] = {}

    def has_open_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def get_open_position(self, symbol: str) -> Optional[OpenPosition]:
        return self.positions.get(symbol)

    def get_open_position_side(self, symbol: str) -> Optional[SideType]:
        pos = self.positions.get(symbol)
        return pos.side if pos is not None else None

    def open_positions_count(self) -> int:
        return len(self.positions)

    def get_all_open_positions(self) -> Dict[str, OpenPosition]:
        return dict(self.positions)

    def get_equity_usd(self) -> float:
        return self.balance_usd

    def get_free_margin_usd(self) -> float:
        locked = sum(p.margin_used_usd for p in self.positions.values())
        return max(0.0, self.balance_usd - locked)

    def submit_close_market(self, symbol: str, reason: str = "OPPOSITE_SIGNAL") -> bool:
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return False
        self.logger.info(f"[MOCK CLOSE] {symbol} {pos.side} qty={pos.qty} reason={reason}")
        return True

    def submit_market_with_bracket(self, plan: OrderPlan) -> bool:
        if plan.symbol in self.positions:
            return False
        margin = plan.notional_usd / float(self.leverage)
        fee = plan.notional_usd * self.taker_fee_rate
        if margin + fee > self.get_free_margin_usd():
            self.logger.warning(f"[MOCK] Недостаточно маржи для {plan.symbol} {plan.side}")
            return False

        self.balance_usd -= fee
        self.positions[plan.symbol] = OpenPosition(
            symbol=plan.symbol,
            side=plan.side,
            qty=plan.qty,
            entry_price=plan.entry_ref_price,
            entry_ts_ms=plan.signal_ts_ms,
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
            margin_used_usd=margin,
            entry_fee_usd=fee,
        )
        self.logger.info(
            f"[MOCK ORDER] {plan.symbol} {plan.side} qty={plan.qty} ref={plan.entry_ref_price} "
            f"SL={plan.stop_loss} TP={plan.take_profit}"
        )
        return True

    def submit_scale_in_market(self, plan: OrderPlan) -> bool:
        pos = self.positions.get(plan.symbol)
        if pos is None or pos.side != plan.side or plan.qty <= 0.0:
            return False
        add_margin = plan.notional_usd / float(self.leverage)
        add_fee = plan.notional_usd * self.taker_fee_rate
        if add_margin + add_fee > self.get_free_margin_usd():
            return False
        self.balance_usd -= add_fee
        tot_qty = pos.qty + plan.qty
        pos.entry_price = ((pos.qty * pos.entry_price) + (plan.qty * plan.entry_ref_price)) / tot_qty
        pos.qty = tot_qty
        pos.margin_used_usd += add_margin
        pos.entry_fee_usd += add_fee
        self.logger.info(f"[MOCK SCALE-IN] {plan.symbol} {plan.side} +{plan.qty} avg_entry={pos.entry_price}")
        return True
