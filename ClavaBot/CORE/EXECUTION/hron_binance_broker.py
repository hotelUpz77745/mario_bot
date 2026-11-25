# Path: ClavaBot/CORE/EXECUTION/hron_binance_broker.py
# Role: Боевой брокер Binance Futures в режиме HEDGE (адаптирован из HRON_3/cron3/API/BINANCE/client_prod.py, мультиаккаунт)

from __future__ import annotations

import hashlib
import hmac
import os
import time
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence
import requests

from ClavaBot.c_log import UnifiedLogger
from ClavaBot.CORE.EXECUTION.base_broker import ExecutionBroker
from ClavaBot.CORE.models import ClosedTrade, OpenPosition, OrderPlan, SideType
from ClavaBot.CORE.spec_manager import SpecManager


HttpTransportFn = Callable[[str, str, Dict[str, str], Optional[Dict[str, Any]], float], Any]


def load_dotenv_file(env_path: str | os.PathLike[str]) -> int:
    """Лёгкий парсер .env (без внешних зависимостей): заполняет только отсутствующие переменные окружения."""
    from pathlib import Path
    p = Path(env_path)
    if not p.exists():
        return 0
    loaded = 0
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        val = v.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val
            loaded += 1
    return loaded


def format_decimal_str(val: Any) -> str:
    """Точный формат числа без научной нотации e/E (из HRON_3/cron3/API/BINANCE/client_prod.py)."""
    if isinstance(val, int):
        return str(val)
    d = Decimal(str(val)).normalize()
    if d == d.to_integral():
        return format(d, "f")
    return format(d, "f").rstrip("0").rstrip(".")


@dataclass(frozen=True, slots=True)
class HronBinanceConfig:
    """Параметры боевого подключения Binance Futures (строго HEDGE Mode, без смены плеч на бирже)."""
    base_url: str
    recv_window_ms: int
    timeout_sec: float
    time_slack_sec: float
    api_rate_limit_sec: float
    working_type: str
    warmup_days: int
    dry_run: bool
    account_cache_ttl_sec: float
    leverage_budget: int
    taker_fee_rate: float

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "HronBinanceConfig":
        return cls(
            base_url=str(raw["base_url"]).rstrip("/"),
            recv_window_ms=int(raw["recv_window_ms"]),
            timeout_sec=float(raw["timeout_sec"]),
            time_slack_sec=float(raw["time_slack_sec"]),
            api_rate_limit_sec=float(raw["api_rate_limit_sec"]),
            working_type=str(raw["working_type"]).upper().strip(),
            warmup_days=int(raw["warmup_days"]),
            dry_run=bool(raw["dry_run"]),
            account_cache_ttl_sec=float(raw["account_cache_ttl_sec"]),
            leverage_budget=int(raw["leverage_budget"]),
            taker_fee_rate=float(raw["taker_fee_rate"]),
        )


class HronBinanceRestAdapter:
    """
    Проверенный REST-адаптер Binance Futures HEDGE Mode (по контракту HRON_3/cron3/API/BINANCE/client_prod.py):
    - мультиаккаунт через api_key_name / api_secret_name из .env;
    - rate limiter, HMAC-SHA256 подпись, time_slack_sec, _format_decimal;
    - БЕЗ вызовов set_leverage / set_margin_type (плечо и Hedge Mode заданы на бирже);
    - поддержка MARKET, STOP_MARKET (closePosition=true, positionSide=LONG|SHORT), cancel_orders_for_side.
    """

    def __init__(
        self,
        cfg: HronBinanceConfig,
        api_key_name: str,
        api_secret_name: str,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        transport: Optional[HttpTransportFn] = None,
    ):
        self.cfg = cfg
        self.api_key_name = api_key_name.strip()
        self.api_secret_name = api_secret_name.strip()
        self.api_key: str = api_key if api_key is not None else os.environ.get(self.api_key_name, "")
        self.api_secret: str = api_secret if api_secret is not None else os.environ.get(self.api_secret_name, "")
        self._session = requests.Session()
        self._transport = transport
        self._last_send_mono: float = 0.0

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
        if self._transport is None and self.cfg.api_rate_limit_sec > 0.0:
            elapsed = time.monotonic() - self._last_send_mono
            if elapsed < self.cfg.api_rate_limit_sec:
                time.sleep(self.cfg.api_rate_limit_sec - elapsed)
            self._last_send_mono = time.monotonic()

        safe_params: Dict[str, Any] = dict(params) if params else {}
        headers: Dict[str, str] = {}
        url = f"{self.cfg.base_url}{path}"

        if signed:
            if not self.api_key or not self.api_secret:
                raise RuntimeError(
                    f"API credentials missing in env ({self.api_key_name} / {self.api_secret_name})"
                )
            safe_params["timestamp"] = int((time.time() - self.cfg.time_slack_sec) * 1000)
            safe_params["recvWindow"] = self.cfg.recv_window_ms
            query = urllib.parse.urlencode(safe_params, safe=",")
            sig = hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
            safe_params["signature"] = sig
            headers["X-MBX-APIKEY"] = self.api_key

        fn = self._transport if self._transport is not None else self._default_transport
        return fn(method.upper(), url, headers, safe_params, self.cfg.timeout_sec)

    def get_exchange_info(self) -> Dict[str, Any]:
        return self._request("GET", "/fapi/v1/exchangeInfo", signed=False)

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

    def fetch_account_info(self) -> Dict[str, Any]:
        return self._request("GET", "/fapi/v2/account", signed=True)

    def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"symbol": symbol.upper()} if symbol else None
        res = self._request("GET", "/fapi/v1/openOrders", params=params, signed=True)
        return res if isinstance(res, list) else []

    def place_hedge_market_order(self, symbol: str, side: str, position_side: SideType, qty: float) -> Dict[str, Any]:
        """В HEDGE Mode Binance запрещает reduceOnly: закрытие LONG — side=SELL + positionSide=LONG."""
        return self._request(
            "POST",
            "/fapi/v1/order",
            params={
                "symbol": symbol.upper(),
                "side": side.upper(),
                "positionSide": position_side,
                "type": "MARKET",
                "quantity": format_decimal_str(abs(qty)),
                "newOrderRespType": "RESULT",
            },
            signed=True,
        )

    def place_hedge_stop_market(
        self,
        symbol: str,
        exit_side: str,
        position_side: SideType,
        stop_price: float,
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/fapi/v1/order",
            params={
                "symbol": symbol.upper(),
                "side": exit_side.upper(),
                "positionSide": position_side,
                "type": "STOP_MARKET",
                "stopPrice": format_decimal_str(stop_price),
                "closePosition": "true",
                "workingType": self.cfg.working_type,
                "priceProtect": "true",
            },
            signed=True,
        )

    def cancel_orders_for_side(self, symbol: str, position_side: SideType) -> int:
        """Отменяет открытые ордера (вкл. STOP_MARKET) строго для своей стороны positionSide (безопасно в HEDGE)."""
        orders = self.fetch_open_orders(symbol)
        ids = [int(o["orderId"]) for o in orders if str(o.get("positionSide", "")).upper() == position_side]
        if not ids:
            return 0
        try:
            self._request(
                "DELETE",
                "/fapi/v1/batchOrders",
                params={"symbol": symbol.upper(), "orderIdList": f"[{','.join(str(i) for i in ids)}]"},
                signed=True,
            )
        except RuntimeError as exc:
            if "-2011" not in str(exc):
                raise
        return len(ids)


class HronBinanceProdBroker(ExecutionBroker):
    """
    Боевой HEDGE-брокер ClavaBot (на базе HRON_3 REST-контракта + ADR-27 расчётный баланс):
    - не вызывает set_leverage (на бирже может стоять 20x; бюджет плеча 2x держит PositionSizer);
    - поддерживает мультиаккаунт (свои api_key_name/api_secret_name на стратегию);
    - передаёт закрытые сделки и тики в полиморфный LiveAnalyticsManager.
    """

    def __init__(
        self,
        cfg: HronBinanceConfig,
        spec_manager: SpecManager,
        rest_adapter: HronBinanceRestAdapter,
        start_balance_usd: float,
        analytics: Optional[Any] = None,
    ):
        self.logger = UnifiedLogger(f"HronProdBroker[{rest_adapter.api_key_name}]")
        self.cfg = cfg
        self.spec_manager = spec_manager
        self.rest = rest_adapter
        self.analytics = analytics

        self.start_balance_usd: float = float(start_balance_usd)
        self.realized_balance_usd: float = float(start_balance_usd)
        self.positions: Dict[str, OpenPosition] = {}
        self.last_prices: Dict[str, float] = {}
        self._exchange_available_margin_usd: Optional[float] = None
        self._last_acc_mono: float = 0.0

    def on_price_tick(self, symbol: str, price: float) -> None:
        if price > 0.0:
            self.last_prices[symbol.upper().strip()] = float(price)

    def refresh_exchange_margin(self, force: bool = False) -> None:
        if self.cfg.dry_run:
            return
        now_m = time.monotonic()
        if not force and (now_m - self._last_acc_mono) < self.cfg.account_cache_ttl_sec:
            return
        acc = self.rest.fetch_account_info()
        self._exchange_available_margin_usd = float(acc["availableBalance"])
        self._last_acc_mono = now_m

    def reconcile_positions(self, symbols: Sequence[str]) -> int:
        if self.cfg.dry_run:
            self.logger.info("[DRY_RUN] Reconcile skipped (virtual HEDGE state active)")
            return len(self.positions)
        wanted = {s.upper().strip() for s in symbols}
        acc = self.rest.fetch_account_info()
        self._exchange_available_margin_usd = float(acc["availableBalance"])
        self._last_acc_mono = time.monotonic()
        open_orders = self.rest.fetch_open_orders()

        stop_map: Dict[tuple[str, str], float] = {}
        for o in open_orders:
            sym = str(o.get("symbol", "")).upper()
            pside = str(o.get("positionSide", "")).upper()
            if sym in wanted and str(o.get("type", "")) == "STOP_MARKET" and pside in ("LONG", "SHORT"):
                stop_map[(sym, pside)] = float(o["stopPrice"])

        rebuilt: Dict[str, OpenPosition] = {}
        now_ms = int(time.time() * 1000)
        for row in acc.get("positions", []):
            sym = str(row.get("symbol", "")).upper()
            if sym not in wanted:
                continue
            amt = float(row.get("positionAmt", 0.0))
            if abs(amt) <= 0.0:
                continue
            pside_raw = str(row.get("positionSide", "")).upper()
            side: SideType = "LONG" if (pside_raw == "LONG" or (pside_raw not in ("LONG", "SHORT") and amt > 0)) else "SHORT"
            qty = abs(amt)
            entry_px = float(row.get("entryPrice", 0.0))
            rebuilt[sym] = OpenPosition(
                symbol=sym,
                side=side,
                qty=qty,
                entry_price=entry_px,
                entry_ts_ms=now_ms,
                stop_loss=stop_map.get((sym, side)),
                take_profit=None,
                margin_used_usd=(qty * entry_px) / float(self.cfg.leverage_budget),
                entry_fee_usd=0.0,
            )
        self.positions = rebuilt
        self.logger.info(f"Reconciled HEDGE positions: {len(self.positions)} open")
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

    def _calc_unrealized(self) -> float:
        unr = 0.0
        for sym, pos in self.positions.items():
            px = self.last_prices.get(sym, pos.entry_price)
            unr += ((px - pos.entry_price) * pos.qty) if pos.side == "LONG" else ((pos.entry_price - px) * pos.qty)
        return unr

    def get_equity_usd(self) -> float:
        """ADR-27: расчётное эквити бота (start + realized + funding + unrealized), защищённое от ручных переводов."""
        return max(0.0, self.realized_balance_usd + self._calc_unrealized())

    def get_free_margin_usd(self) -> float:
        locked = sum(p.margin_used_usd for p in self.positions.values())
        bot_free = max(0.0, self.get_equity_usd() - locked)
        if not self.cfg.dry_run:
            self.refresh_exchange_margin(force=False)
            if self._exchange_available_margin_usd is not None:
                return min(bot_free, max(0.0, self._exchange_available_margin_usd))
        return bot_free

    def submit_close_market(self, symbol: str, reason: str = "OPPOSITE_SIGNAL") -> bool:
        sym = symbol.upper()
        pos = self.positions.get(sym)
        if pos is None:
            return False
        close_side = "SELL" if pos.side == "LONG" else "BUY"
        now_ms = int(time.time() * 1000)

        if self.cfg.dry_run:
            exit_px = self.last_prices.get(sym, pos.entry_price)
            self.logger.info(f"[DRY_RUN HEDGE CLOSE] {sym} {close_side} posSide={pos.side} qty={pos.qty} reason={reason}")
        else:
            self.rest.cancel_orders_for_side(sym, pos.side)
            resp = self.rest.place_hedge_market_order(sym, close_side, pos.side, pos.qty)
            exit_px = float(resp.get("avgPrice") or self.last_prices.get(sym, pos.entry_price))
            if exit_px <= 0.0:
                exit_px = self.last_prices.get(sym, pos.entry_price)
            self.refresh_exchange_margin(force=True)
            self.logger.info(f"[LIVE HEDGE CLOSE] {sym} {close_side} posSide={pos.side} qty={pos.qty} avg={exit_px} reason={reason}")

        del self.positions[sym]
        gross = ((exit_px - pos.entry_price) * pos.qty) if pos.side == "LONG" else ((pos.entry_price - exit_px) * pos.qty)
        exit_fee = (exit_px * pos.qty) * self.cfg.taker_fee_rate
        self.realized_balance_usd += gross - exit_fee
        net = gross - pos.entry_fee_usd - exit_fee

        if self.analytics is not None:
            self.analytics.record_closed_trade(
                ClosedTrade(
                    symbol=sym,
                    side=pos.side,
                    qty=pos.qty,
                    entry_price=pos.entry_price,
                    exit_price=exit_px,
                    entry_ts_ms=pos.entry_ts_ms,
                    exit_ts_ms=now_ms,
                    stop_loss=pos.stop_loss,
                    take_profit=pos.take_profit,
                    gross_pnl_usd=gross,
                    fees_usd=pos.entry_fee_usd + exit_fee,
                    net_pnl_usd=net,
                    exit_reason=reason,  # type: ignore[arg-type]
                    margin_used_usd=pos.margin_used_usd,
                ),
                current_equity_usd=self.get_equity_usd(),
            )
        return True

    def submit_market_with_bracket(self, plan: OrderPlan) -> bool:
        sym = plan.symbol.upper()
        if sym in self.positions:
            return False
        order_side = "BUY" if plan.side == "LONG" else "SELL"
        exit_side = "SELL" if plan.side == "LONG" else "BUY"
        sl_rounded = self.spec_manager.round_price(sym, plan.stop_loss) if plan.stop_loss is not None else None
        margin = plan.notional_usd / float(self.cfg.leverage_budget)
        if margin > self.get_free_margin_usd():
            self.logger.warning(f"Insufficient margin budget for {sym} {plan.side}")
            return False

        if self.cfg.dry_run:
            fill_px = self.last_prices.get(sym, plan.entry_ref_price)
            fill_qty = plan.qty
            self.logger.info(f"[DRY_RUN HEDGE OPEN] {sym} {order_side} posSide={plan.side} qty={fill_qty} SL={sl_rounded}")
        else:
            resp = self.rest.place_hedge_market_order(sym, order_side, plan.side, plan.qty)
            fill_qty = float(resp.get("executedQty") or plan.qty)
            fill_px = float(resp.get("avgPrice") or plan.entry_ref_price) or plan.entry_ref_price
            if sl_rounded is not None:
                self.rest.place_hedge_stop_market(sym, exit_side, plan.side, sl_rounded)
            self.refresh_exchange_margin(force=True)
            self.logger.info(f"[LIVE HEDGE OPEN] {sym} {order_side} posSide={plan.side} qty={fill_qty} avg={fill_px} SL={sl_rounded}")

        entry_fee = (fill_qty * fill_px) * self.cfg.taker_fee_rate
        self.realized_balance_usd -= entry_fee
        self.positions[sym] = OpenPosition(
            symbol=sym,
            side=plan.side,
            qty=fill_qty,
            entry_price=fill_px,
            entry_ts_ms=plan.signal_ts_ms,
            stop_loss=sl_rounded,
            take_profit=plan.take_profit,
            margin_used_usd=(fill_qty * fill_px) / float(self.cfg.leverage_budget),
            entry_fee_usd=entry_fee,
        )
        return True
