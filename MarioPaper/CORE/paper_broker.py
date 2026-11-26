# Path: MarioPaper/CORE/paper_broker.py
# Role: Брокер бумажной торговли MarioPaper (псевдоордера, системные SL/TP в игровой лупе, 8h funding, MarioRuntimeStateStore)

from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional, Sequence

from MarioBot.ANALYTICS.live_analytics import LiveAnalyticsManager
from MarioBot.c_log import UnifiedLogger
from MarioBot.CORE.EXECUTION.base_broker import ExecutionBroker
from MarioBot.CORE.EXECUTION.runtime_state_store import MarioRuntimeStateStore
from MarioBot.CORE.models import ClosedTrade, OpenPosition, OrderPlan, SideType


class PaperExecutionBroker(ExecutionBroker):
    """
    Исполняет псевдоордера по живым тикам из общего IPC-стрима цен (без плеча в расчётах):
    - taker-комиссия (0.05%) + проскальзывание (0.01%) на вход и выход;
    - виртуальные SL и TP проверяются системным триггером check_virtual_triggers (в игровой лупе каждые ~0.1с);
    - реальный 8h funding rate Binance по открытым позициям;
    - пер-символьный рантайм через общий MarioRuntimeStateStore (CFG/runtime/<slot_id>/<sym>.json).
    """

    def __init__(
        self,
        start_balance_usd: float,
        taker_fee_rate: float,
        slippage_rate: float,
        slot_id: str,
        runtime_store: MarioRuntimeStateStore,
        analytics: Optional[LiveAnalyticsManager] = None,
        on_trade_event: Optional[Callable[[str], None]] = None,
    ):
        self.logger = UnifiedLogger("MarioPaperBroker")
        self.start_balance_usd = float(start_balance_usd)
        self.taker_fee_rate = float(taker_fee_rate)
        self.slippage_rate = float(slippage_rate)
        self.slot_id = str(slot_id)
        self.runtime_store = runtime_store
        self.analytics = analytics
        self.on_trade_event = on_trade_event

        self.balance_usd: float = float(start_balance_usd)
        self.positions: Dict[str, OpenPosition] = {}
        self.last_prices: Dict[str, float] = {}
        self._last_funding_bucket: int = -1
        self._tracked_symbols: List[str] = []

    def restore_and_sync(self, symbols: Sequence[str], fallback_sl_fn: Optional[object] = None) -> int:
        wanted = [s.upper().strip() for s in symbols]
        self._tracked_symbols = list(wanted)
        meta = self.runtime_store.load_slot_meta(self.slot_id, self.start_balance_usd)
        self.balance_usd = float(meta["realized_balance_usd"])
        self._last_funding_bucket = int(meta["last_funding_bucket"])
        self.positions = self.runtime_store.load_slot_positions(self.slot_id, wanted)
        self.runtime_store.sync_slot(
            slot_id=self.slot_id,
            symbols=wanted,
            positions=self.positions,
            start_balance_usd=self.start_balance_usd,
            realized_balance_usd=self.balance_usd,
            last_funding_bucket=self._last_funding_bucket,
        )
        self.logger.info(
            f"[PAPER] Runtime restored: balance=${self.balance_usd:.2f}, open_positions={len(self.positions)}"
        )
        return len(self.positions)

    def reconcile_positions(self, symbols: Sequence[str], fallback_sl_fn: Optional[object] = None) -> int:
        return self.restore_and_sync(symbols, fallback_sl_fn=fallback_sl_fn)

    def _persist_symbol(self, symbol: str) -> None:
        self.runtime_store.save_symbol_position(self.slot_id, symbol, self.positions.get(symbol))
        self.runtime_store.save_slot_meta(
            self.slot_id, self.start_balance_usd, self.balance_usd, self._last_funding_bucket
        )

    def on_price_tick(self, symbol: str, price: float, event_time_ms: Optional[int] = None) -> None:
        """Обновляет рыночную цену тика (проверка виртуальных SL/TP выполняется главной игровой лупой)."""
        if price <= 0.0:
            return
        self.last_prices[symbol.upper().strip()] = float(price)

    def check_virtual_triggers(self, now_ms: Optional[int] = None) -> List[ClosedTrade]:
        """Системная проверка виртуальных SL и TP по живым ценам (вызывается в игровой лупе каждые ~0.1с)."""
        ts_ms = int(now_ms) if now_ms is not None else int(time.time() * 1000)
        closed: List[ClosedTrade] = []
        for sym, pos in list(self.positions.items()):
            px = self.last_prices.get(sym)
            if px is None or px <= 0.0:
                continue
            reason: Optional[str] = None
            if pos.stop_loss is not None:
                if (pos.side == "LONG" and px <= pos.stop_loss) or (pos.side == "SHORT" and px >= pos.stop_loss):
                    reason = "STOP_LOSS"
            if reason is None and pos.take_profit is not None:
                if (pos.side == "LONG" and px >= pos.take_profit) or (pos.side == "SHORT" and px <= pos.take_profit):
                    reason = "TAKE_PROFIT"
            if reason is not None:
                exec_px = px * (1.0 - self.slippage_rate) if pos.side == "LONG" else px * (1.0 + self.slippage_rate)
                closed.append(self._close_internal(sym, pos, exec_px, ts_ms, reason))
        return closed

    def apply_funding(self, symbol: str, funding_rate: float, mark_price: Optional[float] = None) -> float:
        sym_u = symbol.upper().strip()
        pos = self.positions.get(sym_u)
        if pos is None or funding_rate == 0.0:
            return 0.0
        px = float(mark_price) if (mark_price and mark_price > 0.0) else self.last_prices.get(sym_u, pos.entry_price)
        notional = pos.qty * px
        cashflow = (-notional * funding_rate) if pos.side == "LONG" else (notional * funding_rate)
        self.balance_usd += cashflow
        self._persist_symbol(sym_u)
        if self.analytics is not None:
            self.analytics.record_funding(sym_u, cashflow, self.get_equity_usd())
        return cashflow

    def has_open_position(self, symbol: str) -> bool:
        return symbol.upper() in self.positions

    def get_open_position(self, symbol: str) -> Optional[OpenPosition]:
        return self.positions.get(symbol.upper())

    def get_open_position_side(self, symbol: str) -> Optional[SideType]:
        pos = self.positions.get(symbol.upper())
        return pos.side if pos is not None else None

    def open_positions_count(self) -> int:
        return len(self.positions)

    def get_all_open_positions(self) -> Dict[str, OpenPosition]:
        return dict(self.positions)

    def get_equity_usd(self) -> float:
        unr = 0.0
        for sym, pos in self.positions.items():
            px = self.last_prices.get(sym, pos.entry_price)
            unr += ((px - pos.entry_price) * pos.qty) if pos.side == "LONG" else ((pos.entry_price - px) * pos.qty)
        return max(0.0, self.balance_usd + unr)

    def get_free_margin_usd(self) -> float:
        return self.get_equity_usd()

    def submit_close_market(self, symbol: str, reason: str = "OPPOSITE_SIGNAL") -> bool:
        sym_u = symbol.upper()
        pos = self.positions.get(sym_u)
        if pos is None:
            return False
        raw_px = self.last_prices.get(sym_u, pos.entry_price)
        exec_px = raw_px * (1.0 - self.slippage_rate) if pos.side == "LONG" else raw_px * (1.0 + self.slippage_rate)
        self._close_internal(sym_u, pos, exec_px, int(time.time() * 1000), reason)
        return True

    def submit_market_with_bracket(self, plan: OrderPlan) -> bool:
        sym_u = plan.symbol.upper()
        if sym_u in self.positions or self.get_equity_usd() <= 0.0:
            return False
        raw_px = self.last_prices.get(sym_u, plan.entry_ref_price)
        fill_px = raw_px * (1.0 + self.slippage_rate) if plan.side == "LONG" else raw_px * (1.0 - self.slippage_rate)
        notional = plan.qty * fill_px
        fee = notional * self.taker_fee_rate

        self.balance_usd -= fee
        self.positions[sym_u] = OpenPosition(
            symbol=sym_u,
            side=plan.side,
            qty=plan.qty,
            entry_price=fill_px,
            entry_ts_ms=plan.signal_ts_ms,
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
            margin_used_usd=0.0,
            entry_fee_usd=fee,
        )
        self._persist_symbol(sym_u)
        if self.analytics is not None:
            self.analytics.update_open_positions(self.positions, self.last_prices, self.get_equity_usd())
            self.analytics.flush()
        if self.on_trade_event is not None:
            self.on_trade_event(
                f"📥 <b>[PAPER] OPEN {sym_u} {plan.side}</b>\n"
                f"Qty: <code>{plan.qty}</code> @ <code>{fill_px:.6g}</code> (Notional: <code>${notional:.2f}</code>)\n"
                f"Virtual SL: <code>{plan.stop_loss}</code> | Virtual TP: <code>{plan.take_profit}</code>"
            )
        self.logger.info(
            f"[PAPER OPEN] {sym_u} {plan.side} qty={plan.qty} fill={fill_px:.6f} virtual_SL={plan.stop_loss}"
        )
        return True

    def _close_internal(
        self,
        symbol: str,
        pos: OpenPosition,
        exit_px: float,
        exit_ts_ms: int,
        reason: str,
    ) -> ClosedTrade:
        del self.positions[symbol]
        gross = ((exit_px - pos.entry_price) * pos.qty) if pos.side == "LONG" else ((pos.entry_price - exit_px) * pos.qty)
        exit_fee = (exit_px * pos.qty) * self.taker_fee_rate
        self.balance_usd += gross - exit_fee
        total_fees = pos.entry_fee_usd + exit_fee
        net_pnl = gross - total_fees
        self._persist_symbol(symbol)

        trade = ClosedTrade(
            symbol=symbol,
            side=pos.side,
            qty=pos.qty,
            entry_price=pos.entry_price,
            exit_price=exit_px,
            entry_ts_ms=pos.entry_ts_ms,
            exit_ts_ms=exit_ts_ms,
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit,
            gross_pnl_usd=gross,
            fees_usd=total_fees,
            net_pnl_usd=net_pnl,
            exit_reason=reason,  # type: ignore[arg-type]
            margin_used_usd=0.0,
        )
        if self.analytics is not None:
            self.analytics.record_closed_trade(trade, self.get_equity_usd())
        if self.on_trade_event is not None:
            self.on_trade_event(
                f"📤 <b>[PAPER] CLOSE {symbol} {pos.side}</b> ({reason})\n"
                f"Вход: <code>{pos.entry_price:.6g}</code> → Выход: <code>{exit_px:.6g}</code>\n"
                f"Net PnL: <b>${net_pnl:+.2f}</b> | Equity: <code>${self.get_equity_usd():,.2f}</code>"
            )
        self.logger.info(
            f"[PAPER CLOSE] {symbol} {pos.side} exit={exit_px:.6f} reason={reason} net=${net_pnl:+.2f}"
        )
        return trade
