# Path: MarioBot/API/BINANCE/rest_client.py
# Role: Чистый REST-адаптер Binance USD-M Futures (HEDGE Mode, HMAC-SHA256, rate limit, public & signed endpoints)

from __future__ import annotations

import hashlib
import hmac
import os
import time
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

import requests

from MarioBot.CORE.models import SideType


HttpTransportFn = Callable[[str, str, Dict[str, str], Optional[Dict[str, Any]], float], Any]


def format_decimal_str(val: Any) -> str:
    """Точный формат числа без научной нотации e/E для ордеров Binance Futures."""
    if isinstance(val, int):
        return str(val)
    d = Decimal(str(val)).normalize()
    if d == d.to_integral():
        return format(d, "f")
    return format(d, "f").rstrip("0").rstrip(".")


@dataclass(frozen=True, slots=True)
class BinanceRestConfig:
    """Конфигурация REST-подключения Binance USD-M Futures (строго HEDGE Mode, без плеча в расчётах)."""
    base_url: str
    recv_window_ms: int
    timeout_sec: float
    time_slack_sec: float
    api_rate_limit_sec: float
    warmup_days: int
    dry_run: bool
    account_cache_ttl_sec: float
    taker_fee_rate: float

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "BinanceRestConfig":
        return cls(
            base_url=str(raw["base_url"]).rstrip("/"),
            recv_window_ms=int(raw["recv_window_ms"]),
            timeout_sec=float(raw["timeout_sec"]),
            time_slack_sec=float(raw["time_slack_sec"]),
            api_rate_limit_sec=float(raw["api_rate_limit_sec"]),
            warmup_days=int(raw["warmup_days"]),
            dry_run=bool(raw["dry_run"]),
            account_cache_ttl_sec=float(raw["account_cache_ttl_sec"]),
            taker_fee_rate=float(raw["taker_fee_rate"]),
        )


class BinanceFuturesRestClient:
    """
    Изолированный REST-клиент Binance USD-M Futures (HEDGE Mode):
    - мультиаккаунт через api_key_name / api_secret_name из .env;
    - rate limiter, HMAC-SHA256 подпись, time_slack_sec, format_decimal_str;
    - БЕЗ вызовов set_leverage / set_margin_type;
    - физические ордера только MARKET (positionSide=LONG|SHORT); виртуальные SL/TP проверяет игровая лупа.
    """

    def __init__(
        self,
        cfg: BinanceRestConfig,
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

    # ---------- Public market data endpoints ----------

    def get_exchange_info(self) -> Dict[str, Any]:
        return self._request("GET", "/fapi/v1/exchangeInfo", signed=False)

    def get_exchange_instruments(self) -> List[Dict[str, Any]]:
        """Возвращает список инструментов из /fapi/v1/exchangeInfo (для Контура 1 детектора делистинга)."""
        payload = self.get_exchange_info()
        symbols = payload.get("symbols") if isinstance(payload, dict) else None
        if not isinstance(symbols, list):
            return []
        return [row for row in symbols if isinstance(row, dict) and isinstance(row.get("symbol"), str)]

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

    # ---------- Private signed trading endpoints ----------

    def fetch_account_info(self) -> Dict[str, Any]:
        return self._request("GET", "/fapi/v2/account", signed=True)

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
