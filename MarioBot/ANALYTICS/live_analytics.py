# Path: MarioBot/ANALYTICS/live_analytics.py
# Role: Полиморфный домен аналитики (единый для боевого MarioBot и бумажного MarioPaper)

from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from MarioBot.CORE.models import ClosedTrade, OpenPosition


class LiveAnalyticsManager:
    """
    Универсальный полиморфный менеджер аналитики (PROD и PAPER):
    - ведёт analytics.json, trades_ledger.csv и читаемую сводку summary_report.txt;
    - считает все ключевые метрики Mario: чистый PnL, ROI%, MaxDD (% и $), MinEq, Winrate, PF,
      время в сделке (min/avg/max), средний профит ($ и %), лучшая/худшая сделка,
      макс. исполненный SL% и макс. профитный ход TP%, разбивка по монетам и фандинг;
    - сохраняет состояние между перезапусками бота.
    """

    def __init__(
        self,
        strategy_id: str,
        mode: str,
        start_balance_usd: float,
        storage_dir: Path,
        symbols: Optional[List[str]] = None,
    ):
        self.strategy_id = str(strategy_id)
        self.mode = str(mode).upper().strip()
        self.start_balance_usd = float(start_balance_usd)
        self.storage_dir = Path(storage_dir)
        self.stats_dir = self.storage_dir / "STATS"
        self.ledgers_dir = self.storage_dir / "LEDGERS"
        self.stats_dir.mkdir(parents=True, exist_ok=True)
        self.ledgers_dir.mkdir(parents=True, exist_ok=True)

        self.json_path = self.stats_dir / f"analytics_{self.strategy_id}.json"
        self.ledger_path = self.ledgers_dir / f"trades_ledger_{self.strategy_id}.csv"
        self.report_path = self.stats_dir / f"summary_{self.strategy_id}.txt"

        self.state: Dict[str, Any] = self._load_or_init(symbols or [])

    def _load_or_init(self, symbols: List[str]) -> Dict[str, Any]:
        if self.json_path.exists():
            try:
                loaded = json.loads(self.json_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and "start_balance_usd" in loaded:
                    for sym in symbols:
                        loaded["per_coin"].setdefault(sym.upper(), self._empty_coin())
                    return loaded
            except Exception:
                pass
        now_ms = int(time.time() * 1000)
        return {
            "strategy_id": self.strategy_id,
            "mode": self.mode,
            "started_ts_ms": now_ms,
            "updated_ts_ms": now_ms,
            "start_balance_usd": self.start_balance_usd,
            "current_equity_usd": self.start_balance_usd,
            "realized_pnl_usd": 0.0,
            "unrealized_pnl_usd": 0.0,
            "total_fees_usd": 0.0,
            "total_funding_usd": 0.0,
            "net_pnl_usd": 0.0,
            "roi_pct": 0.0,
            "peak_equity_usd": self.start_balance_usd,
            "min_equity_usd": self.start_balance_usd,
            "max_drawdown_pct": 0.0,
            "max_drawdown_usd": 0.0,
            "trades_total": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "winrate_pct": 0.0,
            "gross_wins_usd": 0.0,
            "gross_losses_usd": 0.0,
            "profit_factor": 0.0,
            "avg_trade_pnl_usd": 0.0,
            "avg_trade_roi_pct": 0.0,
            "_sum_trade_roi_pct": 0.0,
            "hold_min_h": None,
            "hold_avg_h": 0.0,
            "hold_max_h": 0.0,
            "_sum_hold_h": 0.0,
            "best_trade_usd": 0.0,
            "worst_trade_usd": 0.0,
            "max_sl_exec_pct": 0.0,
            "max_tp_move_pct": 0.0,
            "open_longs": 0,
            "open_shorts": 0,
            "open_portfolio_risk_pct": 0.0,
            "exit_reasons": {},
            "per_coin": {s.upper(): self._empty_coin() for s in symbols},
        }

    @staticmethod
    def _empty_coin() -> Dict[str, Any]:
        return {
            "trades": 0,
            "wins": 0,
            "winrate_pct": 0.0,
            "net_pnl_usd": 0.0,
            "unrealized_pnl_usd": 0.0,
            "fees_usd": 0.0,
            "funding_usd": 0.0,
            "sl_count": 0,
            "active_side": None,
            "active_sl": None,
        }

    def record_funding(self, symbol: str, cashflow_usd: float, current_equity_usd: float) -> None:
        st = self.state
        sym_u = symbol.upper().strip()
        st["total_funding_usd"] = round(float(st["total_funding_usd"]) + float(cashflow_usd), 4)
        coin = st["per_coin"].setdefault(sym_u, self._empty_coin())
        coin["funding_usd"] = round(float(coin["funding_usd"]) + float(cashflow_usd), 4)
        self._update_equity_extrema(current_equity_usd)
        self.flush()

    def record_closed_trade(self, trade: ClosedTrade, current_equity_usd: float) -> None:
        st = self.state
        sym_u = trade.symbol.upper().strip()
        st["trades_total"] += 1
        if trade.net_pnl_usd > 0.0:
            st["winning_trades"] += 1
            st["gross_wins_usd"] = round(float(st["gross_wins_usd"]) + trade.net_pnl_usd, 4)
        else:
            st["losing_trades"] += 1
            st["gross_losses_usd"] = round(float(st["gross_losses_usd"]) + abs(trade.net_pnl_usd), 4)

        st["realized_pnl_usd"] = round(float(st["realized_pnl_usd"]) + trade.net_pnl_usd, 4)
        st["total_fees_usd"] = round(float(st["total_fees_usd"]) + trade.fees_usd, 4)
        st["winrate_pct"] = round((st["winning_trades"] / st["trades_total"]) * 100.0, 2)
        st["profit_factor"] = (
            round(float(st["gross_wins_usd"]) / float(st["gross_losses_usd"]), 2)
            if float(st["gross_losses_usd"]) > 0.0
            else (999.0 if float(st["gross_wins_usd"]) > 0.0 else 0.0)
        )

        # Время в сделке и % хода от ТВХ
        hold_h = max(0.0, (trade.exit_ts_ms - trade.entry_ts_ms) / 3_600_000.0)
        st["_sum_hold_h"] = float(st["_sum_hold_h"]) + hold_h
        st["hold_avg_h"] = round(st["_sum_hold_h"] / st["trades_total"], 2)
        st["hold_min_h"] = round(hold_h, 2) if st["hold_min_h"] is None else round(min(float(st["hold_min_h"]), hold_h), 2)
        st["hold_max_h"] = round(max(float(st["hold_max_h"]), hold_h), 2)

        notional = max(1e-9, trade.qty * trade.entry_price)
        trade_roi_pct = (trade.net_pnl_usd / notional) * 100.0
        st["_sum_trade_roi_pct"] = float(st["_sum_trade_roi_pct"]) + trade_roi_pct
        st["avg_trade_pnl_usd"] = round(float(st["realized_pnl_usd"]) / st["trades_total"], 2)
        st["avg_trade_roi_pct"] = round(st["_sum_trade_roi_pct"] / st["trades_total"], 2)

        st["best_trade_usd"] = round(max(float(st["best_trade_usd"]), trade.net_pnl_usd), 2)
        st["worst_trade_usd"] = round(min(float(st["worst_trade_usd"]), trade.net_pnl_usd), 2)

        move_pct = (
            ((trade.exit_price - trade.entry_price) / trade.entry_price) * 100.0
            if trade.side == "LONG"
            else ((trade.entry_price - trade.exit_price) / trade.entry_price) * 100.0
        )
        if move_pct > float(st["max_tp_move_pct"]):
            st["max_tp_move_pct"] = round(move_pct, 2)
        if trade.exit_reason == "STOP_LOSS":
            sl_pct = abs(trade.exit_price - trade.entry_price) / max(1e-9, trade.entry_price) * 100.0
            if sl_pct > float(st["max_sl_exec_pct"]):
                st["max_sl_exec_pct"] = round(sl_pct, 2)

        ex_map = st["exit_reasons"]
        ex_map[trade.exit_reason] = int(ex_map.get(trade.exit_reason, 0)) + 1

        coin = st["per_coin"].setdefault(sym_u, self._empty_coin())
        coin["trades"] += 1
        if trade.net_pnl_usd > 0.0:
            coin["wins"] += 1
        coin["winrate_pct"] = round((coin["wins"] / coin["trades"]) * 100.0, 2)
        coin["net_pnl_usd"] = round(float(coin["net_pnl_usd"]) + trade.net_pnl_usd, 4)
        coin["fees_usd"] = round(float(coin["fees_usd"]) + trade.fees_usd, 4)
        if trade.exit_reason == "STOP_LOSS":
            coin["sl_count"] += 1
        coin["active_side"] = None
        coin["active_sl"] = None
        coin["unrealized_pnl_usd"] = 0.0

        self._update_equity_extrema(current_equity_usd)
        self._append_ledger_row(trade, move_pct, hold_h, current_equity_usd)
        self.flush()

    def update_open_positions(
        self,
        open_positions: Dict[str, OpenPosition],
        last_prices: Dict[str, float],
        current_equity_usd: float,
    ) -> None:
        st = self.state
        longs = 0
        shorts = 0
        total_unr = 0.0
        open_risk_usd = 0.0

        active_syms = set(open_positions.keys())
        for sym, c in st["per_coin"].items():
            if sym not in active_syms:
                c["active_side"] = None
                c["active_sl"] = None
                c["unrealized_pnl_usd"] = 0.0

        for sym, pos in open_positions.items():
            sym_u = sym.upper().strip()
            px = last_prices.get(sym_u, pos.entry_price)
            unr = ((px - pos.entry_price) * pos.qty) if pos.side == "LONG" else ((pos.entry_price - px) * pos.qty)
            total_unr += unr
            if pos.side == "LONG":
                longs += 1
            else:
                shorts += 1
            if pos.stop_loss is not None:
                open_risk_usd += abs(pos.entry_price - pos.stop_loss) * pos.qty
            coin = st["per_coin"].setdefault(sym_u, self._empty_coin())
            coin["active_side"] = pos.side
            coin["active_sl"] = pos.stop_loss
            coin["unrealized_pnl_usd"] = round(unr, 4)

        st["open_longs"] = longs
        st["open_shorts"] = shorts
        st["unrealized_pnl_usd"] = round(total_unr, 4)
        st["open_portfolio_risk_pct"] = (
            round((open_risk_usd / current_equity_usd) * 100.0, 2) if current_equity_usd > 0.0 else 0.0
        )
        self._update_equity_extrema(current_equity_usd)

    def _update_equity_extrema(self, current_equity_usd: float) -> None:
        st = self.state
        eq = float(current_equity_usd)
        st["current_equity_usd"] = round(eq, 4)
        st["net_pnl_usd"] = round(eq - self.start_balance_usd, 4)
        st["roi_pct"] = (
            round(((eq - self.start_balance_usd) / self.start_balance_usd) * 100.0, 2)
            if self.start_balance_usd > 0.0
            else 0.0
        )
        if eq > float(st["peak_equity_usd"]):
            st["peak_equity_usd"] = round(eq, 4)
        if eq < float(st["min_equity_usd"]):
            st["min_equity_usd"] = round(eq, 4)
        peak = float(st["peak_equity_usd"])
        dd_usd = max(0.0, peak - eq)
        dd_pct = (dd_usd / peak) * 100.0 if peak > 0.0 else 0.0
        if dd_usd > float(st["max_drawdown_usd"]):
            st["max_drawdown_usd"] = round(dd_usd, 4)
        if dd_pct > float(st["max_drawdown_pct"]):
            st["max_drawdown_pct"] = round(dd_pct, 2)
        st["updated_ts_ms"] = int(time.time() * 1000)

    def _append_ledger_row(self, trade: ClosedTrade, move_pct: float, hold_h: float, eq_after: float) -> None:
        is_new = not self.ledger_path.exists()
        with self.ledger_path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, delimiter=";")
            if is_new:
                w.writerow([
                    "OpenUTC", "CloseUTC", "Symbol", "Side", "Qty", "Entry", "Exit",
                    "MovePct", "HoldHours", "Reason", "FeesUSD", "NetPnLUSD", "EquityUSD",
                ])
            o_str = datetime.fromtimestamp(trade.entry_ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            c_str = datetime.fromtimestamp(trade.exit_ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            w.writerow([
                o_str, c_str, trade.symbol, trade.side, round(trade.qty, 6),
                round(trade.entry_price, 6), round(trade.exit_price, 6),
                round(move_pct, 2), round(hold_h, 2), trade.exit_reason,
                round(trade.fees_usd, 4), round(trade.net_pnl_usd, 4), round(eq_after, 4),
            ])

    def render_summary_text(self) -> str:
        st = self.state
        lines = [
            f"=== MARIO ANALYTICS [{st['mode']}] | {st['strategy_id']} ===",
            f"Balance: ${st['start_balance_usd']:.2f} -> ${st['current_equity_usd']:.2f} "
            f"| Net PnL: ${st['net_pnl_usd']:+.2f} ({st['roi_pct']:+.2f}%)",
            f"MaxDD: {st['max_drawdown_pct']:.2f}% (${st['max_drawdown_usd']:.2f}) "
            f"| MinEq: ${st['min_equity_usd']:.2f} | PeakEq: ${st['peak_equity_usd']:.2f}",
            f"Trades: {st['trades_total']} | WR: {st['winrate_pct']:.1f}% | PF: {st['profit_factor']:.2f} "
            f"| Fees: -${abs(st['total_fees_usd']):.2f} | Funding: ${st['total_funding_usd']:+.2f}",
            f"Hold (h): min={st['hold_min_h']} avg={st['hold_avg_h']} max={st['hold_max_h']} "
            f"| AvgTrade: ${st['avg_trade_pnl_usd']:+.2f} ({st['avg_trade_roi_pct']:+.2f}%)",
            f"Best: ${st['best_trade_usd']:+.2f} | Worst: ${st['worst_trade_usd']:+.2f} "
            f"| MaxTP%: {st['max_tp_move_pct']:+.2f}% | MaxSL%: -{st['max_sl_exec_pct']:.2f}%",
            f"Open: {st['open_longs']}L / {st['open_shorts']}S | OpenRisk: {st['open_portfolio_risk_pct']:.2f}%",
        ]
        return "\n".join(lines) + "\n"

    def flush(self) -> None:
        self.json_path.write_text(json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8")
        self.report_path.write_text(self.render_summary_text(), encoding="utf-8")
