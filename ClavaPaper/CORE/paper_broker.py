# Path: ClavaPaper/CORE/paper_broker.py
# Role: Брокер бумажной торговли ClavaPaper (псевдоордера по живым тикам, тиковый SL, 8h funding, персистентность)

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from ClavaBot.ANALYTICS.live_analytics import LiveAnalyticsManager
from ClavaBot.c_log import UnifiedLogger
from ClavaBot.CORE.EXECUTION.base_broker import ExecutionBroker
from ClavaBot.CORE.models import ClosedTrade, OpenPosition, OrderPlan, SideType


class PaperExecutionBroker(ExecutionBroker):
    """
    Исполняет псевдоордера по живым тикам из общего IPC-стрима цен:
    - учитывает taker-комиссию (0.05%) и проскальзывание (0.01%) на вход и выход;
    - проверяет виртуальный 5*ATR Stop-Loss на КАЖДОМ живом тике;
    - применяет реальный 8h funding rate Binance по открытым позициям;
    - пишет все сделки и живой equity в полиморфный LiveAnalyticsManager и сохраняет состояние на диск.
    """

    def __init__(
        self,
        start_balance_usd: float,
        leverage_budget: int,
        taker_fee_rate: float,
        slippage_rate: float,
        state_path: Path,
        analytics: Optional[LiveAnalyticsManager] = None,
    ):
        self.logger = UnifiedLogger("ClavaPaperBroker")
        self.start_balance_usd = float(start_balance_usd)
        self.leverage_budget = int(leverage_budget)
        self.taker_fee_rate = float(taker_fee_rate)
        self.slippage_rate = float(slippage_rate)
        self.state_path = Path(state_path)
        self.analytics = analytics

        self.balance_usd: float = float(start_balance_usd)
        self.positions: Dict[str, OpenPosition] = {}
        self.last_prices: Dict[str, float] = {}
        self._load_state()

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.balance_usd = float(raw["balance_usd"])
            for sym, p in raw.get("positions", {}).items():
                self.positions[sym.upper()] = OpenPosition(
                    symbol=sym.upper(),
                    side=p["side"],
                    qty=float(p["qty"]),
                    entry_price=float(p["entry_price"]),
                    entry_ts_ms=int(p["entry_ts_ms"]),
                    stop_loss=float(p["stop_loss"]) if p["stop_loss"] is not None else None,
                    take_profit=float(p["take_profit"]) if p["take_profit"] is not None else None,
                    margin_used_usd=float(p["margin_used_usd"]),
                    entry_fee_usd=float(p["entry_fee_usd"]),
                )
            self.logger.info(
                f"[PAPER] Restored state: balance=${self.balance_usd:.2f}, open_positions={len(self.positions)}"
            )
        except Exception as exc:
            self.logger.warning(f"[PAPER] Failed to restore state ({exc}); starting fresh")

    def save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_ts_ms": int(time.time() * 1000),
            "balance_usd": round(self.balance_usd, 6),
            "positions": {
                sym: {
                    "side": p.side,
                    "qty": p.qty,
                    "entry_price": p.entry_price,
                    "entry_ts_ms": p.entry_ts_ms,
                    "stop_loss": p.stop_loss,
                    "take_profit": p.take_profit,
                    "margin_used_usd": p.margin_used_usd,
                    "entry_fee_usd": p.entry_fee_usd,
                }
                for sym, p in self.positions.items()
            },
        }
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def on_price_tick(self, symbol: str, price: float, event_time_ms: int) -> Optional[ClosedTrade]:
        """Обновляет рыночную цену и проверяет срабатывание виртуального Stop-Loss на живом тике."""
        if price <= 0.0:
            return None
        sym_u = symbol.upper().strip()
        self.last_prices[sym_u] = float(price)

        pos = self.positions.get(sym_u)
        if pos is None:
            return None

        sl_hit = False
        if pos.stop_loss is not None:
            sl_hit = (pos.side == "LONG" and price <= pos.stop_loss) or (
                pos.side == "SHORT" and price >= pos.stop_loss
            )
        if sl_hit:
            exec_px = price * (1.0 - self.slippage_rate) if pos.side == "LONG" else price * (1.0 + self.slippage_rate)
            return self._close_internal(sym_u, pos, exec_px, event_time_ms, "STOP_LOSS")

        if self.analytics is not None:
            self.analytics.update_open_positions(self.positions, self.last_prices, self.get_equity_usd())
        return None

    def apply_funding(self, symbol: str, funding_rate: float, mark_price: Optional[float] = None) -> float:
        sym_u = symbol.upper().strip()
        pos = self.positions.get(sym_u)
        if pos is None or funding_rate == 0.0:
            return 0.0
        px = float(mark_price) if (mark_price and mark_price > 0.0) else self.last_prices.get(sym_u, pos.entry_price)
        notional = pos.qty * px
        cashflow = (-notional * funding_rate) if pos.side == "LONG" else (notional * funding_rate)
        self.balance_usd += cashflow
        self.save_state()
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
        return self.balance_usd + unr

    def get_free_margin_usd(self) -> float:
        locked = sum(p.margin_used_usd for p in self.positions.values())
        return max(0.0, self.get_equity_usd() - locked)

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
        if sym_u in self.positions:
            return False
        raw_px = self.last_prices.get(sym_u, plan.entry_ref_price)
        fill_px = raw_px * (1.0 + self.slippage_rate) if plan.side == "LONG" else raw_px * (1.0 - self.slippage_rate)
        notional = plan.qty * fill_px
        margin = notional / float(self.leverage_budget)
        fee = notional * self.taker_fee_rate
        if margin + fee > self.get_free_margin_usd():
            self.logger.warning(f"[PAPER] Insufficient margin for {sym_u} {plan.side}")
            return False

        self.balance_usd -= fee
        self.positions[sym_u] = OpenPosition(
            symbol=sym_u,
            side=plan.side,
            qty=plan.qty,
            entry_price=fill_px,
            entry_ts_ms=plan.signal_ts_ms,
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
            margin_used_usd=margin,
            entry_fee_usd=fee,
        )
        self.save_state()
        if self.analytics is not None:
            self.analytics.update_open_positions(self.positions, self.last_prices, self.get_equity_usd())
            self.analytics.flush()
        self.logger.info(
            f"[PAPER OPEN] {sym_u} {plan.side} qty={plan.qty} fill={fill_px:.6f} SL={plan.stop_loss}"
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
            margin_used_usd=pos.margin_used_usd,
        )
        self.save_state()
        if self.analytics is not None:
            self.analytics.record_closed_trade(trade, self.get_equity_usd())
        self.logger.info(
            f"[PAPER CLOSE] {symbol} {pos.side} exit={exit_px:.6f} reason={reason} net=${net_pnl:+.2f}"
        )
        return trade
