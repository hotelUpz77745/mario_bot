# Path: MarioBot/CORE/EXECUTION/binance_broker.py
# Role: Боевой HEDGE-брокер Binance Futures (виртуальные SL/TP в игровой лупе, без плеча в расчётах, ресторация рантайма)

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from MarioBot.API.BINANCE.rest_client import BinanceFuturesRestClient, BinanceRestConfig
from MarioBot.c_log import UnifiedLogger
from MarioBot.CORE.EXECUTION.base_broker import ExecutionBroker
from MarioBot.CORE.EXECUTION.runtime_state_store import MarioRuntimeStateStore
from MarioBot.CORE.models import ClosedTrade, OpenPosition, OrderPlan, SideType
from MarioBot.CORE.spec_manager import SpecManager


FallbackSlFn = Callable[[str, SideType, float], Optional[float]]


class BinanceFuturesProdBroker(ExecutionBroker):
    """
    Боевой HEDGE-брокер MarioBot (чистый слой исполнения без HTTP-транспорта — клиент приходит через DI):
    - физические ордера на биржу ТОЛЬКО MARKET (вход и выход через BinanceFuturesRestClient);
    - Stop-Loss и Take-Profit — виртуальные системные триггеры, проверяемые в игровой лупе по живым тикам;
    - плечо НЕ участвует в расчётах объёма и маржи;
    - пер-символьная запись рантайма (MarioRuntimeStateStore) и простая ресторация перед стартом лупы.
    """

    def __init__(
        self,
        cfg: BinanceRestConfig,
        spec_manager: SpecManager,
        rest_client: BinanceFuturesRestClient,
        start_balance_usd: float,
        slot_id: str = "default",
        runtime_store: Optional[MarioRuntimeStateStore] = None,
        analytics: Optional[Any] = None,
        on_trade_event: Optional[Callable[[str], None]] = None,
    ):
        self.logger = UnifiedLogger(f"BinanceProdBroker[{rest_client.api_key_name}]")
        self.cfg = cfg
        self.spec_manager = spec_manager
        self.rest = rest_client
        self.slot_id = str(slot_id)
        self.runtime_store = runtime_store
        self.analytics = analytics
        self.on_trade_event = on_trade_event

        self.start_balance_usd: float = float(start_balance_usd)
        self.realized_balance_usd: float = float(start_balance_usd)
        self.positions: Dict[str, OpenPosition] = {}
        self.last_prices: Dict[str, float] = {}
        self._last_funding_bucket: int = -1
        self._tracked_symbols: List[str] = []

    def on_price_tick(self, symbol: str, price: float) -> None:
        if price > 0.0:
            self.last_prices[symbol.upper().strip()] = float(price)

    def check_virtual_triggers(self, now_ms: Optional[int] = None) -> List[ClosedTrade]:
        """
        Системная проверка виртуальных SL и TP по живым тикам (вызывается главной игровой лупой каждые ~0.1с).
        При пробое уровня отправляет MARKET-закрытие в HEDGE Mode.
        """
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
                trade = self._close_market_internal(sym, reason=reason, now_ms=now_ms)
                if trade is not None:
                    closed.append(trade)
        return closed

    def restore_and_sync(
        self,
        symbols: Sequence[str],
        fallback_sl_fn: Optional[FallbackSlFn] = None,
    ) -> int:
        """
        Простая и надёжная ресторация рантайма перед главным торговым циклом:
        1. Загружает рантайм с диска (CFG/runtime/<slot_id>/<sym>.json) в память.
        2. В LIVE-режиме (dry_run=false) запрашивает реальные позиции с биржи:
           - если позиции на бирже НЕТ -> сбрасывает символ в дефолтную структуру;
           - если позиция на бирже ЕСТЬ -> подсасывает и корректирует qty (и entry_price при отсутствии на диске).
        3. Синхронизирует память обратно на диск.
        """
        wanted = [s.upper().strip() for s in symbols]
        self._tracked_symbols = list(wanted)

        if self.runtime_store is not None:
            meta = self.runtime_store.load_slot_meta(self.slot_id, self.start_balance_usd)
            self.realized_balance_usd = float(meta["realized_balance_usd"])
            self._last_funding_bucket = int(meta["last_funding_bucket"])
            self.positions = self.runtime_store.load_slot_positions(self.slot_id, wanted)

        if self.cfg.dry_run:
            self._persist_all(wanted)
            self.logger.info(f"[DRY_RUN] Runtime restored from disk: {len(self.positions)} open")
            return len(self.positions)

        acc = self.rest.fetch_account_info()
        live_map: Dict[str, tuple[SideType, float, float]] = {}
        wanted_set = set(wanted)
        for row in acc.get("positions", []):
            sym = str(row.get("symbol", "")).upper()
            if sym not in wanted_set:
                continue
            amt = float(row.get("positionAmt", 0.0))
            if abs(amt) <= 0.0:
                continue
            pside_raw = str(row.get("positionSide", "")).upper()
            side: SideType = (
                "LONG"
                if (pside_raw == "LONG" or (pside_raw not in ("LONG", "SHORT") and amt > 0))
                else "SHORT"
            )
            live_map[sym] = (side, abs(amt), float(row.get("entryPrice", 0.0)))

        now_ms = int(time.time() * 1000)
        synced: Dict[str, OpenPosition] = {}
        for sym in wanted:
            live = live_map.get(sym)
            if live is None:
                if sym in self.positions:
                    self.logger.info(f"[{sym}] Position absent on exchange -> resetting runtime to default")
                continue
            live_side, live_qty, live_entry = live
            cached = self.positions.get(sym)
            if cached is not None and cached.side == live_side:
                entry_px = live_entry if live_entry > 0.0 else cached.entry_price
                sl_px = cached.stop_loss
                if sl_px is None and fallback_sl_fn is not None and entry_px > 0.0:
                    sl_px = fallback_sl_fn(sym, live_side, entry_px)
                synced[sym] = OpenPosition(
                    symbol=sym,
                    side=live_side,
                    qty=live_qty,
                    entry_price=entry_px,
                    entry_ts_ms=cached.entry_ts_ms,
                    stop_loss=sl_px,
                    take_profit=cached.take_profit,
                    margin_used_usd=0.0,
                    entry_fee_usd=cached.entry_fee_usd,
                )
            else:
                sl_px = fallback_sl_fn(sym, live_side, live_entry) if (fallback_sl_fn and live_entry > 0.0) else None
                synced[sym] = OpenPosition(
                    symbol=sym,
                    side=live_side,
                    qty=live_qty,
                    entry_price=live_entry,
                    entry_ts_ms=now_ms,
                    stop_loss=sl_px,
                    take_profit=None,
                    margin_used_usd=0.0,
                    entry_fee_usd=0.0,
                )
                self.logger.info(
                    f"[{sym}] Adopted live exchange position: {live_side} qty={live_qty} entry={live_entry} virtual_SL={sl_px}"
                )

        self.positions = synced
        self._persist_all(wanted)
        self.logger.info(f"Runtime restored & synced with exchange: {len(self.positions)} open positions")
        return len(self.positions)

    def reconcile_positions(self, symbols: Sequence[str], fallback_sl_fn: Optional[FallbackSlFn] = None) -> int:
        return self.restore_and_sync(symbols, fallback_sl_fn=fallback_sl_fn)

    def _persist_symbol(self, symbol: str) -> None:
        if self.runtime_store is not None:
            self.runtime_store.save_symbol_position(self.slot_id, symbol, self.positions.get(symbol))
            self.runtime_store.save_slot_meta(
                self.slot_id, self.start_balance_usd, self.realized_balance_usd, self._last_funding_bucket
            )

    def _persist_all(self, symbols: Sequence[str]) -> None:
        if self.runtime_store is not None:
            self.runtime_store.sync_slot(
                slot_id=self.slot_id,
                symbols=symbols,
                positions=self.positions,
                start_balance_usd=self.start_balance_usd,
                realized_balance_usd=self.realized_balance_usd,
                last_funding_bucket=self._last_funding_bucket,
            )

    def apply_funding(self, symbol: str, funding_rate: float, mark_price: Optional[float] = None) -> float:
        sym = symbol.upper().strip()
        pos = self.positions.get(sym)
        if pos is None or funding_rate == 0.0:
            return 0.0
        px = float(mark_price) if (mark_price and mark_price > 0.0) else self.last_prices.get(sym, pos.entry_price)
        notional = pos.qty * px
        cashflow = (-notional * funding_rate) if pos.side == "LONG" else (notional * funding_rate)
        self.realized_balance_usd += cashflow
        self._persist_symbol(sym)
        if self.analytics is not None:
            self.analytics.record_funding(sym, cashflow, self.get_equity_usd())
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

    def _calc_unrealized(self) -> float:
        unr = 0.0
        for sym, pos in self.positions.items():
            px = self.last_prices.get(sym, pos.entry_price)
            unr += ((px - pos.entry_price) * pos.qty) if pos.side == "LONG" else ((pos.entry_price - px) * pos.qty)
        return unr

    def get_equity_usd(self) -> float:
        """Расчётное эквити бота (start + realized + funding + unrealized)."""
        return max(0.0, self.realized_balance_usd + self._calc_unrealized())

    def get_free_margin_usd(self) -> float:
        return self.get_equity_usd()

    def submit_close_market(self, symbol: str, reason: str = "OPPOSITE_SIGNAL") -> bool:
        return self._close_market_internal(symbol, reason=reason, now_ms=None) is not None

    def _close_market_internal(
        self, symbol: str, reason: str, now_ms: Optional[int]
    ) -> Optional[ClosedTrade]:
        sym = symbol.upper()
        pos = self.positions.get(sym)
        if pos is None:
            return None
        close_side = "SELL" if pos.side == "LONG" else "BUY"
        ts_ms = int(now_ms) if now_ms is not None else int(time.time() * 1000)

        if self.cfg.dry_run:
            exit_px = self.last_prices.get(sym, pos.entry_price)
            self.logger.info(
                f"[DRY_RUN HEDGE CLOSE] {sym} {close_side} posSide={pos.side} qty={pos.qty} px={exit_px} reason={reason}"
            )
        else:
            resp = self.rest.place_hedge_market_order(sym, close_side, pos.side, pos.qty)
            exit_px = float(resp.get("avgPrice") or self.last_prices.get(sym, pos.entry_price))
            if exit_px <= 0.0:
                exit_px = self.last_prices.get(sym, pos.entry_price)
            self.logger.info(
                f"[LIVE HEDGE CLOSE] {sym} {close_side} posSide={pos.side} qty={pos.qty} avg={exit_px} reason={reason}"
            )

        del self.positions[sym]
        gross = ((exit_px - pos.entry_price) * pos.qty) if pos.side == "LONG" else ((pos.entry_price - exit_px) * pos.qty)
        exit_fee = (exit_px * pos.qty) * self.cfg.taker_fee_rate
        self.realized_balance_usd += gross - exit_fee
        net = gross - pos.entry_fee_usd - exit_fee
        self._persist_symbol(sym)

        trade = ClosedTrade(
            symbol=sym,
            side=pos.side,
            qty=pos.qty,
            entry_price=pos.entry_price,
            exit_price=exit_px,
            entry_ts_ms=pos.entry_ts_ms,
            exit_ts_ms=ts_ms,
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit,
            gross_pnl_usd=gross,
            fees_usd=pos.entry_fee_usd + exit_fee,
            net_pnl_usd=net,
            exit_reason=reason,  # type: ignore[arg-type]
            margin_used_usd=0.0,
        )
        if self.analytics is not None:
            self.analytics.record_closed_trade(trade, current_equity_usd=self.get_equity_usd())
        if self.on_trade_event is not None:
            self.on_trade_event(
                f"📤 <b>CLOSE {sym} {pos.side}</b> ({reason})\n"
                f"Вход: <code>{pos.entry_price:.6g}</code> → Выход: <code>{exit_px:.6g}</code>\n"
                f"Net PnL: <b>${net:+.2f}</b> | Equity: <code>${self.get_equity_usd():,.2f}</code>"
            )
        return trade

    def submit_market_with_bracket(self, plan: OrderPlan) -> bool:
        sym = plan.symbol.upper()
        if sym in self.positions:
            return False
        order_side = "BUY" if plan.side == "LONG" else "SELL"
        sl_rounded = self.spec_manager.round_price(sym, plan.stop_loss) if plan.stop_loss is not None else None
        tp_rounded = self.spec_manager.round_price(sym, plan.take_profit) if plan.take_profit is not None else None

        if self.cfg.dry_run:
            fill_px = self.last_prices.get(sym, plan.entry_ref_price)
            fill_qty = plan.qty
            self.logger.info(
                f"[DRY_RUN HEDGE OPEN] {sym} {order_side} posSide={plan.side} qty={fill_qty} "
                f"px={fill_px} virtual_SL={sl_rounded} virtual_TP={tp_rounded}"
            )
        else:
            resp = self.rest.place_hedge_market_order(sym, order_side, plan.side, plan.qty)
            fill_qty = float(resp.get("executedQty") or plan.qty)
            fill_px = float(resp.get("avgPrice") or plan.entry_ref_price) or plan.entry_ref_price
            self.logger.info(
                f"[LIVE HEDGE OPEN] {sym} {order_side} posSide={plan.side} qty={fill_qty} "
                f"avg={fill_px} virtual_SL={sl_rounded} virtual_TP={tp_rounded}"
            )

        entry_fee = (fill_qty * fill_px) * self.cfg.taker_fee_rate
        self.realized_balance_usd -= entry_fee
        self.positions[sym] = OpenPosition(
            symbol=sym,
            side=plan.side,
            qty=fill_qty,
            entry_price=fill_px,
            entry_ts_ms=plan.signal_ts_ms,
            stop_loss=sl_rounded,
            take_profit=tp_rounded,
            margin_used_usd=0.0,
            entry_fee_usd=entry_fee,
        )
        self._persist_symbol(sym)
        if self.analytics is not None:
            self.analytics.update_open_positions(self.positions, self.last_prices, self.get_equity_usd())
            self.analytics.flush()
        if self.on_trade_event is not None:
            self.on_trade_event(
                f"📥 <b>OPEN {sym} {plan.side}</b>\n"
                f"Qty: <code>{fill_qty}</code> @ <code>{fill_px:.6g}</code> (Notional: <code>${fill_qty * fill_px:.2f}</code>)\n"
                f"Virtual SL: <code>{sl_rounded}</code> | Virtual TP: <code>{tp_rounded}</code>"
            )
        return True
