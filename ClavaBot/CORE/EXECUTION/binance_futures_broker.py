# Path: ClavaBot/CORE/EXECUTION/binance_futures_broker.py
# Role: Боевой брокер Binance Futures USD-M (REST + HMAC-SHA256, MARKET + STOP_MARKET, реконсиляция)

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import urlencode
import requests

from ClavaBot.c_log import UnifiedLogger
from ClavaBot.CORE.EXECUTION.base_broker import ExecutionBroker
from ClavaBot.CORE.models import OpenPosition, OrderPlan, SideType
from ClavaBot.CORE.spec_manager import SpecManager


HttpTransportFn = Callable[[str, str, Dict[str, str], Optional[Dict[str, Any]], float], Any]


@dataclass(frozen=True, slots=True)
class BinanceFuturesConfig:
    """Строго типизированная конфигурация боевого подключения Binance USD-M Futures."""
    base_url: str
    ws_url: str
    api_key_env: str
    api_secret_env: str
    recv_window_ms: int
    timeout_sec: float
    leverage: int
    margin_type: str
    working_type: str
    warmup_days: int
    dry_run: bool
    account_cache_ttl_sec: float
    entry_delay_sec: int

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "BinanceFuturesConfig":
        return cls(
            base_url=str(raw["base_url"]).rstrip("/"),
            ws_url=str(raw["ws_url"]).rstrip("/"),
            api_key_env=str(raw["api_key_env"]),
            api_secret_env=str(raw["api_secret_env"]),
            recv_window_ms=int(raw["recv_window_ms"]),
            timeout_sec=float(raw["timeout_sec"]),
            leverage=int(raw["leverage"]),
            margin_type=str(raw["margin_type"]).upper().strip(),
            working_type=str(raw["working_type"]).upper().strip(),
            warmup_days=int(raw["warmup_days"]),
            dry_run=bool(raw["dry_run"]),
            account_cache_ttl_sec=float(raw["account_cache_ttl_sec"]),
            entry_delay_sec=int(raw["entry_delay_sec"]),
        )


class BinanceFuturesRestClient:
    """Минималистичный REST-клиент Binance USD-M Futures с подписью HMAC-SHA256 и инъекцией транспорта."""

    def __init__(
        self,
        cfg: BinanceFuturesConfig,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        transport: Optional[HttpTransportFn] = None,
    ):
        self.cfg = cfg
        self.api_key: str = api_key if api_key is not None else os.environ.get(cfg.api_key_env, "")
        self.api_secret: str = api_secret if api_secret is not None else os.environ.get(cfg.api_secret_env, "")
        self._session = requests.Session()
        self._transport = transport
        self.time_offset_ms: int = 0

    def _default_transport(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        params: Optional[Dict[str, Any]],
        timeout_sec: float,
    ) -> Any:
        resp = self._session.request(method=method, url=url, headers=headers, params=params, timeout=timeout_sec)
        if resp.status_code >= 400:
            raise RuntimeError(f"Binance API HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False,
    ) -> Any:
        payload: Dict[str, Any] = dict(params) if params else {}
        headers: Dict[str, str] = {}
        if signed:
            if not self.api_key or not self.api_secret:
                raise RuntimeError(
                    f"Binance API credentials missing in env ({self.cfg.api_key_env} / {self.cfg.api_secret_env})"
                )
            payload["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
            payload["recvWindow"] = self.cfg.recv_window_ms
            query = urlencode(payload, doseq=True)
            sig = hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
            payload["signature"] = sig
            headers["X-MBX-APIKEY"] = self.api_key

        url = f"{self.cfg.base_url}{path}"
        fn = self._transport if self._transport is not None else self._default_transport
        return fn(method.upper(), url, headers, payload, self.cfg.timeout_sec)

    def sync_server_time(self) -> int:
        data = self._request("GET", "/fapi/v1/time")
        server_ms = int(data["serverTime"])
        local_ms = int(time.time() * 1000)
        self.time_offset_ms = server_ms - local_ms
        return self.time_offset_ms

    def get_exchange_info(self) -> Dict[str, Any]:
        return self._request("GET", "/fapi/v1/exchangeInfo")

    def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time_ms: Optional[int] = None,
        end_time_ms: Optional[int] = None,
        limit: int = 1500,
    ) -> List[List[Any]]:
        params: Dict[str, Any] = {"symbol": symbol.upper(), "interval": interval, "limit": int(limit)}
        if start_time_ms is not None:
            params["startTime"] = int(start_time_ms)
        if end_time_ms is not None:
            params["endTime"] = int(end_time_ms)
        return self._request("GET", "/fapi/v1/klines", params=params, signed=False)

    def get_premium_index(self, symbol: str) -> Dict[str, Any]:
        return self._request("GET", "/fapi/v1/premiumIndex", params={"symbol": symbol.upper()}, signed=False)

    def get_account(self) -> Dict[str, Any]:
        return self._request("GET", "/fapi/v2/account", signed=True)

    def get_position_risk(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"symbol": symbol.upper()} if symbol else None
        return self._request("GET", "/fapi/v2/positionRisk", params=params, signed=True)

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"symbol": symbol.upper()} if symbol else None
        return self._request("GET", "/fapi/v1/openOrders", params=params, signed=True)

    def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/fapi/v1/leverage",
            params={"symbol": symbol.upper(), "leverage": int(leverage)},
            signed=True,
        )

    def set_margin_type(self, symbol: str, margin_type: str) -> Optional[Dict[str, Any]]:
        try:
            return self._request(
                "POST",
                "/fapi/v1/marginType",
                params={"symbol": symbol.upper(), "marginType": margin_type.upper()},
                signed=True,
            )
        except RuntimeError as exc:
            if "-4046" in str(exc):  # No need to change margin type
                return None
            raise

    def place_order(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/fapi/v1/order", params=params, signed=True)

    def cancel_all_open_orders(self, symbol: str) -> Dict[str, Any]:
        return self._request("DELETE", "/fapi/v1/allOpenOrders", params={"symbol": symbol.upper()}, signed=True)


class BinanceFuturesExecutionBroker(ExecutionBroker):
    """
    Боевой брокер Binance Futures (One-Way Mode, Cross Margin):
    - читает живое equity (totalMarginBalance) и free_margin (availableBalance);
    - ставит MARKET + STOP_MARKET (closePosition=true, workingType=MARK_PRICE);
    - при закрытии (SIGNAL_EXIT / OPPOSITE_SIGNAL) сначала снимает все ордера символа;
    - поддерживает безопасный dry_run, sync_symbol_specs и reconcile_positions после рестарта.
    """

    def __init__(
        self,
        cfg: BinanceFuturesConfig,
        spec_manager: SpecManager,
        client: Optional[BinanceFuturesRestClient] = None,
        dry_run_balance_usd: float = 10000.0,
    ):
        self.logger = UnifiedLogger("BinanceFuturesBroker")
        self.cfg = cfg
        self.spec_manager = spec_manager
        self.client = client if client is not None else BinanceFuturesRestClient(cfg)

        self.positions: Dict[str, OpenPosition] = {}
        self.stop_order_ids: Dict[str, int] = {}
        self._equity_usd: float = float(dry_run_balance_usd)
        self._free_margin_usd: float = float(dry_run_balance_usd)
        self._last_account_refresh_mono: float = 0.0

    def sync_symbol_specs(self, symbols: Sequence[str]) -> int:
        info = self.client.get_exchange_info()
        wanted = {s.upper().strip() for s in symbols}
        updated = 0
        for row in info.get("symbols", []):
            sym = str(row.get("symbol", "")).upper().strip()
            if sym not in wanted:
                continue
            f_map = {str(f["filterType"]): f for f in row.get("filters", []) if isinstance(f, dict) and "filterType" in f}
            pf = f_map["PRICE_FILTER"]
            ls = f_map["LOT_SIZE"]
            mn = f_map.get("MIN_NOTIONAL", {})
            min_notional = float(mn["notional"]) if "notional" in mn else 5.0
            self.spec_manager.upsert_spec(
                symbol=sym,
                tick_size=str(pf["tickSize"]),
                step_size=str(ls["stepSize"]),
                min_qty=str(ls["minQty"]),
                min_notional_usd=min_notional,
            )
            updated += 1
        self.logger.info(f"Synced Binance symbol specs: {updated}/{len(wanted)}")
        return updated

    def configure_margin_and_leverage(self, symbols: Sequence[str]) -> None:
        if self.cfg.dry_run:
            self.logger.info(f"[DRY_RUN] Skip margin/leverage API setup ({self.cfg.margin_type}, {self.cfg.leverage}x)")
            return
        for sym in symbols:
            self.client.set_margin_type(sym, self.cfg.margin_type)
            self.client.set_leverage(sym, self.cfg.leverage)

    def refresh_account(self, force: bool = False) -> None:
        if self.cfg.dry_run:
            locked = sum(p.margin_used_usd for p in self.positions.values())
            self._free_margin_usd = max(0.0, self._equity_usd - locked)
            return
        now_m = time.monotonic()
        if not force and (now_m - self._last_account_refresh_mono) < self.cfg.account_cache_ttl_sec:
            return
        acc = self.client.get_account()
        self._equity_usd = float(acc["totalMarginBalance"])
        self._free_margin_usd = float(acc["availableBalance"])
        self._last_account_refresh_mono = now_m

    def reconcile_positions(self, symbols: Sequence[str]) -> int:
        if self.cfg.dry_run:
            self.logger.info("[DRY_RUN] Reconcile skipped (virtual state active)")
            return len(self.positions)
        wanted = {s.upper().strip() for s in symbols}
        risk_rows = self.client.get_position_risk()
        open_orders = self.client.get_open_orders()

        stop_by_sym: Dict[str, tuple[float, int]] = {}
        for o in open_orders:
            sym = str(o.get("symbol", "")).upper()
            if sym in wanted and str(o.get("type", "")) == "STOP_MARKET":
                stop_by_sym[sym] = (float(o["stopPrice"]), int(o["orderId"]))

        rebuilt: Dict[str, OpenPosition] = {}
        self.stop_order_ids.clear()
        now_ms = int(time.time() * 1000)
        for row in risk_rows:
            sym = str(row.get("symbol", "")).upper()
            if sym not in wanted:
                continue
            amt = float(row["positionAmt"])
            if abs(amt) <= 0.0:
                continue
            side: SideType = "LONG" if amt > 0.0 else "SHORT"
            qty = abs(amt)
            entry_px = float(row["entryPrice"])
            lev = max(1, int(float(row["leverage"])))
            sl_info = stop_by_sym.get(sym)
            sl_px = sl_info[0] if sl_info is not None else None
            if sl_info is not None:
                self.stop_order_ids[sym] = sl_info[1]
            rebuilt[sym] = OpenPosition(
                symbol=sym,
                side=side,
                qty=qty,
                entry_price=entry_px,
                entry_ts_ms=now_ms,
                stop_loss=sl_px,
                take_profit=None,
                margin_used_usd=(qty * entry_px) / float(lev),
                entry_fee_usd=0.0,
            )

        self.positions = rebuilt
        self.refresh_account(force=True)
        self.logger.info(f"Reconciled live positions: {len(self.positions)} open, equity=${self._equity_usd:.2f}")
        return len(self.positions)

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
        self.refresh_account(force=False)
        return self._equity_usd

    def get_free_margin_usd(self) -> float:
        self.refresh_account(force=False)
        return self._free_margin_usd

    def submit_close_market(self, symbol: str, reason: str = "OPPOSITE_SIGNAL") -> bool:
        sym = symbol.upper()
        pos = self.positions.get(sym)
        if pos is None:
            return False
        close_side = "SELL" if pos.side == "LONG" else "BUY"
        if self.cfg.dry_run:
            del self.positions[sym]
            self.stop_order_ids.pop(sym, None)
            self.logger.info(f"[DRY_RUN CLOSE] {sym} {close_side} qty={pos.qty} reason={reason}")
            return True

        # Сначала гарантированно снимаем STOP_MARKET / TP ордера, чтобы не было призрачных позиций
        self.client.cancel_all_open_orders(sym)
        self.client.place_order(
            {
                "symbol": sym,
                "side": close_side,
                "type": "MARKET",
                "quantity": str(pos.qty),
                "reduceOnly": "true",
            }
        )
        del self.positions[sym]
        self.stop_order_ids.pop(sym, None)
        self.refresh_account(force=True)
        self.logger.info(f"[LIVE CLOSE] {sym} {close_side} qty={pos.qty} reason={reason}")
        return True

    def submit_market_with_bracket(self, plan: OrderPlan) -> bool:
        sym = plan.symbol.upper()
        if sym in self.positions:
            return False
        order_side = "BUY" if plan.side == "LONG" else "SELL"
        exit_side = "SELL" if plan.side == "LONG" else "BUY"
        sl_rounded = self.spec_manager.round_price(sym, plan.stop_loss) if plan.stop_loss is not None else None
        tp_rounded = self.spec_manager.round_price(sym, plan.take_profit) if plan.take_profit is not None else None

        if self.cfg.dry_run:
            margin = plan.notional_usd / float(self.cfg.leverage)
            if margin > self.get_free_margin_usd():
                return False
            self.positions[sym] = OpenPosition(
                symbol=sym,
                side=plan.side,
                qty=plan.qty,
                entry_price=plan.entry_ref_price,
                entry_ts_ms=plan.signal_ts_ms,
                stop_loss=sl_rounded,
                take_profit=tp_rounded,
                margin_used_usd=margin,
                entry_fee_usd=0.0,
            )
            self.logger.info(f"[DRY_RUN OPEN] {sym} {order_side} qty={plan.qty} SL={sl_rounded} TP={tp_rounded}")
            return True

        entry_resp = self.client.place_order(
            {
                "symbol": sym,
                "side": order_side,
                "type": "MARKET",
                "quantity": str(plan.qty),
            }
        )
        filled_qty = float(entry_resp.get("executedQty") or plan.qty)
        avg_px = float(entry_resp.get("avgPrice") or plan.entry_ref_price)
        if avg_px <= 0.0:
            avg_px = plan.entry_ref_price

        if sl_rounded is not None:
            sl_resp = self.client.place_order(
                {
                    "symbol": sym,
                    "side": exit_side,
                    "type": "STOP_MARKET",
                    "stopPrice": str(sl_rounded),
                    "closePosition": "true",
                    "workingType": self.cfg.working_type,
                    "priceProtect": "true",
                }
            )
            if "orderId" in sl_resp:
                self.stop_order_ids[sym] = int(sl_resp["orderId"])

        if tp_rounded is not None:
            self.client.place_order(
                {
                    "symbol": sym,
                    "side": exit_side,
                    "type": "TAKE_PROFIT_MARKET",
                    "stopPrice": str(tp_rounded),
                    "closePosition": "true",
                    "workingType": self.cfg.working_type,
                    "priceProtect": "true",
                }
            )

        self.positions[sym] = OpenPosition(
            symbol=sym,
            side=plan.side,
            qty=filled_qty,
            entry_price=avg_px,
            entry_ts_ms=plan.signal_ts_ms,
            stop_loss=sl_rounded,
            take_profit=tp_rounded,
            margin_used_usd=(filled_qty * avg_px) / float(self.cfg.leverage),
            entry_fee_usd=0.0,
        )
        self.refresh_account(force=True)
        self.logger.info(f"[LIVE OPEN] {sym} {order_side} qty={filled_qty} avg={avg_px} SL={sl_rounded}")
        return True

    def submit_scale_in_market(self, plan: OrderPlan) -> bool:
        sym = plan.symbol.upper()
        pos = self.positions.get(sym)
        if pos is None or pos.side != plan.side or plan.qty <= 0.0:
            return False
        order_side = "BUY" if plan.side == "LONG" else "SELL"
        if not self.cfg.dry_run:
            resp = self.client.place_order(
                {"symbol": sym, "side": order_side, "type": "MARKET", "quantity": str(plan.qty)}
            )
            add_qty = float(resp.get("executedQty") or plan.qty)
            fill_px = float(resp.get("avgPrice") or plan.entry_ref_price) or plan.entry_ref_price
        else:
            add_qty = plan.qty
            fill_px = plan.entry_ref_price

        tot_qty = pos.qty + add_qty
        pos.entry_price = ((pos.qty * pos.entry_price) + (add_qty * fill_px)) / tot_qty
        pos.qty = tot_qty
        pos.margin_used_usd += (add_qty * fill_px) / float(self.cfg.leverage)
        self.refresh_account(force=True)
        return True
