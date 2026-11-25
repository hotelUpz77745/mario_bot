# Path: ClavaBacktest/ENGINE/sim_broker.py
# Role: 1-минутный симулятор брокера (рыночное исполнение, реверс по сигналу, комиссии, SL раньше TP)

from __future__ import annotations

from typing import Dict, List, Optional
from ClavaBot.CORE.EXECUTION.base_broker import ExecutionBroker
from ClavaBot.CORE.models import ClosedTrade, ExitReasonType, OpenPosition, OrderPlan, SideType


class SimExecutionBroker(ExecutionBroker):
    """
    Симулирует портфельный счёт бэктеста на минутном потоке (1m OHLC):
    1. Заявки на закрытие (OPPOSITE_SIGNAL) и новые входы исполняются по Open следующей 1m-свечи.
    2. При активных SL/TP внутри каждой 1m-свечи Stop-Loss проверяется ПЕРВЫМ, Take-Profit — ВТОРЫМ.
    """

    def __init__(
        self,
        start_balance_usd: float,
        leverage: int,
        taker_fee_rate: float,
        maker_fee_rate: float,
        slippage_rate: float,
        tp_slippage_rate: float,
    ):
        self.balance_usd: float = float(start_balance_usd)
        self.leverage: int = int(leverage)
        self.taker_fee_rate: float = float(taker_fee_rate)
        self.maker_fee_rate: float = float(maker_fee_rate)
        self.slippage_rate: float = float(slippage_rate)
        self.tp_slippage_rate: float = float(tp_slippage_rate)

        self.positions: Dict[str, OpenPosition] = {}
        self.pending_closes: Dict[str, ExitReasonType] = {}
        self.pending_orders: Dict[str, OrderPlan] = {}
        self.pending_scale_ins: Dict[str, OrderPlan] = {}
        self.last_close_prices: Dict[str, float] = {}
        self.closed_trades: List[ClosedTrade] = []
        self.total_funding_usd: float = 0.0

    def has_open_position(self, symbol: str) -> bool:
        if symbol in self.pending_orders:
            return True
        return symbol in self.positions and symbol not in self.pending_closes

    def get_open_position(self, symbol: str) -> Optional[OpenPosition]:
        if symbol in self.positions and symbol not in self.pending_closes:
            return self.positions[symbol]
        return None

    def get_open_position_side(self, symbol: str) -> Optional[SideType]:
        if symbol in self.pending_orders:
            return self.pending_orders[symbol].side
        if symbol in self.positions and symbol not in self.pending_closes:
            return self.positions[symbol].side
        return None

    def open_positions_count(self) -> int:
        active = (set(self.positions.keys()) - set(self.pending_closes.keys())) | set(self.pending_orders.keys())
        return len(active)

    def get_all_open_positions(self) -> Dict[str, OpenPosition]:
        return {
            sym: pos for sym, pos in self.positions.items() if sym not in self.pending_closes
        }

    def apply_funding(self, symbol: str, ts_ms: int, funding_rate: float, mark_price: float) -> float:
        """
        Применяет 8-часовой фандинг Binance Futures к открытой позиции:
        - funding_rate > 0: LONG платит, SHORT получает;
        - funding_rate < 0: SHORT платит, LONG получает.
        """
        pos = self.positions.get(symbol)
        if pos is None or mark_price <= 0.0 or funding_rate == 0.0:
            return 0.0
        if pos.entry_ts_ms > ts_ms:
            return 0.0
        notional = pos.qty * mark_price
        cashflow = (-notional * funding_rate) if pos.side == "LONG" else (notional * funding_rate)
        self.balance_usd += cashflow
        self.total_funding_usd += cashflow
        return cashflow

    def get_equity_usd(self) -> float:
        unrealized = 0.0
        for sym, pos in self.positions.items():
            px = self.last_close_prices.get(sym, pos.entry_price)
            if pos.side == "LONG":
                unrealized += (px - pos.entry_price) * pos.qty
            else:
                unrealized += (pos.entry_price - px) * pos.qty
        return self.balance_usd + unrealized

    def get_free_margin_usd(self) -> float:
        locked = sum(
            p.margin_used_usd for sym, p in self.positions.items() if sym not in self.pending_closes
        )
        pending_locked = sum(p.notional_usd / float(self.leverage) for p in self.pending_orders.values())
        return max(0.0, self.get_equity_usd() - locked - pending_locked)

    def submit_close_market(self, symbol: str, reason: str = "OPPOSITE_SIGNAL") -> bool:
        if symbol not in self.positions or symbol in self.pending_closes:
            return False
        self.pending_closes[symbol] = reason  # type: ignore[assignment]
        return True

    def submit_market_with_bracket(self, plan: OrderPlan) -> bool:
        if self.has_open_position(plan.symbol):
            return False
        self.pending_orders[plan.symbol] = plan
        return True

    def submit_scale_in_market(self, plan: OrderPlan) -> bool:
        pos = self.get_open_position(plan.symbol)
        if pos is None or pos.side != plan.side or plan.qty <= 0.0:
            return False
        self.pending_scale_ins[plan.symbol] = plan
        return True

    def fill_pending_at_minute_open(self, symbol: str, ts_ms: int, open_price: float) -> None:
        if open_price <= 0.0:
            return

        # 1. Сначала исполняем закрытие (если был разворотный сигнал)
        close_reason = self.pending_closes.pop(symbol, None)
        if close_reason is not None and symbol in self.positions:
            pos = self.positions[symbol]
            exit_px = (
                open_price * (1.0 - self.slippage_rate)
                if pos.side == "LONG"
                else open_price * (1.0 + self.slippage_rate)
            )
            self._close_position(symbol, pos, exit_px, ts_ms, self.taker_fee_rate, close_reason)

        # 2. Затем исполняем новый рыночный вход
        plan = self.pending_orders.pop(symbol, None)
        if plan is not None and symbol not in self.positions:
            self._execute_open_plan(plan, ts_ms, open_price)

        # 3. Опциональный добор существующей позиции по тренду (scale-in 20% -> 100%)
        scale_plan = self.pending_scale_ins.pop(symbol, None)
        if scale_plan is not None and symbol in self.positions:
            self._execute_scale_in(scale_plan, open_price)

    def _execute_open_plan(self, plan: OrderPlan, ts_ms: int, open_price: float) -> None:
        if plan.side == "LONG":
            fill_price = open_price * (1.0 + self.slippage_rate)
            if plan.stop_loss is not None and fill_price <= plan.stop_loss:
                return
        else:
            fill_price = open_price * (1.0 - self.slippage_rate)
            if plan.stop_loss is not None and fill_price >= plan.stop_loss:
                return

        notional = plan.qty * fill_price
        margin = notional / float(self.leverage)
        entry_fee = notional * self.taker_fee_rate
        if margin + entry_fee > self.get_free_margin_usd():
            return

        self.balance_usd -= entry_fee
        self.positions[plan.symbol] = OpenPosition(
            symbol=plan.symbol,
            side=plan.side,
            qty=plan.qty,
            entry_price=fill_price,
            entry_ts_ms=ts_ms,
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
            margin_used_usd=margin,
            entry_fee_usd=entry_fee,
        )

    def _execute_scale_in(self, plan: OrderPlan, open_price: float) -> None:
        pos = self.positions.get(plan.symbol)
        if pos is None or pos.side != plan.side:
            return
        fill_price = (
            open_price * (1.0 + self.slippage_rate)
            if pos.side == "LONG"
            else open_price * (1.0 - self.slippage_rate)
        )
        add_notional = plan.qty * fill_price
        add_margin = add_notional / float(self.leverage)
        add_fee = add_notional * self.taker_fee_rate
        if add_margin + add_fee > self.get_free_margin_usd():
            return

        self.balance_usd -= add_fee
        tot_qty = pos.qty + plan.qty
        pos.entry_price = ((pos.qty * pos.entry_price) + (plan.qty * fill_price)) / tot_qty
        pos.qty = tot_qty
        pos.margin_used_usd += add_margin
        pos.entry_fee_usd += add_fee

    def process_1m_bar(self, symbol: str, ts_ms: int, o: float, h: float, l: float, c: float) -> None:
        self.last_close_prices[symbol] = c
        pos = self.positions.get(symbol)
        if pos is None:
            return

        if pos.side == "LONG":
            if pos.stop_loss is not None and l <= pos.stop_loss:
                raw_exit = min(o, pos.stop_loss)
                exit_price = raw_exit * (1.0 - self.slippage_rate)
                self._close_position(symbol, pos, exit_price, ts_ms, self.taker_fee_rate, "STOP_LOSS")
                return
            if pos.take_profit is not None and h >= pos.take_profit:
                raw_exit = max(o, pos.take_profit)
                exit_price = raw_exit * (1.0 - self.tp_slippage_rate)
                self._close_position(symbol, pos, exit_price, ts_ms, self.maker_fee_rate, "TAKE_PROFIT")
                return
        else:
            if pos.stop_loss is not None and h >= pos.stop_loss:
                raw_exit = max(o, pos.stop_loss)
                exit_price = raw_exit * (1.0 + self.slippage_rate)
                self._close_position(symbol, pos, exit_price, ts_ms, self.taker_fee_rate, "STOP_LOSS")
                return
            if pos.take_profit is not None and l <= pos.take_profit:
                raw_exit = min(o, pos.take_profit)
                exit_price = raw_exit * (1.0 + self.tp_slippage_rate)
                self._close_position(symbol, pos, exit_price, ts_ms, self.maker_fee_rate, "TAKE_PROFIT")
                return

    def force_close_all(self, ts_ms: int) -> None:
        for sym in list(self.positions.keys()):
            pos = self.positions[sym]
            px = self.last_close_prices.get(sym, pos.entry_price)
            exit_px = px * (1.0 - self.slippage_rate) if pos.side == "LONG" else px * (1.0 + self.slippage_rate)
            self._close_position(sym, pos, exit_px, ts_ms, self.taker_fee_rate, "END_OF_SIM")

    def _close_position(
        self,
        symbol: str,
        pos: OpenPosition,
        exit_price: float,
        exit_ts_ms: int,
        exit_fee_rate: float,
        reason: ExitReasonType,
    ) -> None:
        del self.positions[symbol]
        if pos.side == "LONG":
            gross_pnl = (exit_price - pos.entry_price) * pos.qty
        else:
            gross_pnl = (pos.entry_price - exit_price) * pos.qty

        exit_fee = (exit_price * pos.qty) * exit_fee_rate
        self.balance_usd += gross_pnl - exit_fee
        total_fees = pos.entry_fee_usd + exit_fee
        net_pnl = gross_pnl - total_fees

        self.closed_trades.append(
            ClosedTrade(
                symbol=symbol,
                side=pos.side,
                qty=pos.qty,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                entry_ts_ms=pos.entry_ts_ms,
                exit_ts_ms=exit_ts_ms,
                stop_loss=pos.stop_loss,
                take_profit=pos.take_profit,
                gross_pnl_usd=gross_pnl,
                fees_usd=total_fees,
                net_pnl_usd=net_pnl,
                exit_reason=reason,
                margin_used_usd=pos.margin_used_usd,
            )
        )
