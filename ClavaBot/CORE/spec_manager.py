# Path: ClavaBot/CORE/spec_manager.py
# Role: Менеджер спецификаций инструментов (tickSize, stepSize, minQty, minNotional)

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import json
from pathlib import Path
import time
from typing import Any, Callable, Dict, Optional
import requests


DEFAULT_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional_usd: float


class SpecManager:
    """
    Округляет цены и объёмы строго по спецификациям биржи.
    Поддерживает:
    1. Загрузку из отдельного JSON-кэша CACHE/specifications.json (формат Binance exchangeInfo)
       с автоматическим обновлением раз в сутки (ttl_sec=86400) — без хардкода монет в app.json.
    2. Прямую инициализацию словарём для обратной совместимости с бэктестом/тестами.
    """

    def __init__(
        self,
        specs_cfg: Optional[Dict[str, Dict[str, Any]]] = None,
        cache_path: Optional[Path] = None,
        ttl_sec: float = 86400.0,
        exchange_info_url: str = DEFAULT_EXCHANGE_INFO_URL,
    ):
        self._specs: Dict[str, SymbolSpec] = {}
        self.cache_path: Optional[Path] = Path(cache_path) if cache_path is not None else None
        self.ttl_sec: float = float(ttl_sec)
        self.exchange_info_url: str = str(exchange_info_url)
        self._last_loaded_ts: float = 0.0

        if specs_cfg:
            for sym, raw in specs_cfg.items():
                self.upsert_spec(
                    symbol=sym,
                    tick_size=raw["tick_size"],
                    step_size=raw["step_size"],
                    min_qty=raw["min_qty"],
                    min_notional_usd=float(raw["min_notional_usd"]),
                )
            self._last_loaded_ts = time.time()

    @classmethod
    def from_app_config(
        cls,
        bot_dir: Path,
        app_cfg: Dict[str, Any],
        auto_refresh: bool = False,
        fetcher: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> "SpecManager":
        sc = app_cfg["specs_cache"]
        return cls.from_cache_file(
            cache_path=(Path(bot_dir) / str(sc["path"])).resolve(),
            ttl_sec=float(sc["ttl_sec"]),
            auto_refresh=auto_refresh,
            fetcher=fetcher,
            exchange_info_url=str(sc["exchange_info_url"]),
        )

    @classmethod
    def from_cache_file(
        cls,
        cache_path: Path,
        ttl_sec: float = 86400.0,
        auto_refresh: bool = False,
        fetcher: Optional[Callable[[], Dict[str, Any]]] = None,
        exchange_info_url: str = DEFAULT_EXCHANGE_INFO_URL,
    ) -> "SpecManager":
        mgr = cls(
            specs_cfg=None,
            cache_path=cache_path,
            ttl_sec=ttl_sec,
            exchange_info_url=exchange_info_url,
        )
        mgr.load_or_refresh(auto_refresh=auto_refresh, fetcher=fetcher)
        return mgr

    def is_cache_stale(self) -> bool:
        if self.cache_path is None or not self.cache_path.exists():
            return True
        age = time.time() - self.cache_path.stat().st_mtime
        return age >= self.ttl_sec

    def load_or_refresh(
        self,
        auto_refresh: bool = False,
        fetcher: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> int:
        if auto_refresh and self.is_cache_stale():
            self.refresh_from_exchange(fetcher=fetcher)
        if self.cache_path is not None and self.cache_path.exists():
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            count = self._load_exchange_info_payload(raw)
            self._last_loaded_ts = time.time()
            return count
        raise FileNotFoundError(f"Specifications cache file not found: {self.cache_path}")

    def refresh_from_exchange(self, fetcher: Optional[Callable[[], Dict[str, Any]]] = None) -> int:
        if fetcher is not None:
            payload = fetcher()
        else:
            resp = requests.get(self.exchange_info_url, timeout=15.0)
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to fetch Binance exchangeInfo HTTP {resp.status_code}")
            payload = resp.json()

        # Сохраняем компактный, но 100% совместимый с Binance exchangeInfo JSON (все USDT PERPETUAL)
        compact_symbols = []
        for row in payload.get("symbols", []):
            if not isinstance(row, dict) or "symbol" not in row:
                continue
            keep_filters = [
                f for f in row.get("filters", [])
                if isinstance(f, dict) and f.get("filterType") in ("PRICE_FILTER", "LOT_SIZE", "MIN_NOTIONAL")
            ]
            if keep_filters:
                compact_symbols.append(
                    {
                        "symbol": str(row["symbol"]).upper().strip(),
                        "status": row.get("status", "TRADING"),
                        "contractType": row.get("contractType", "PERPETUAL"),
                        "filters": keep_filters,
                    }
                )
        out_payload = {"updated_ts_ms": int(time.time() * 1000), "symbols": compact_symbols}
        if self.cache_path is not None:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(out_payload, indent=2), encoding="utf-8")
        count = self._load_exchange_info_payload(out_payload)
        self._last_loaded_ts = time.time()
        return count

    def _load_exchange_info_payload(self, payload: Dict[str, Any]) -> int:
        loaded = 0
        if "symbols" in payload and isinstance(payload["symbols"], list):
            for row in payload["symbols"]:
                if not isinstance(row, dict) or "symbol" not in row:
                    continue
                f_map = {
                    str(f["filterType"]): f
                    for f in row.get("filters", [])
                    if isinstance(f, dict) and "filterType" in f
                }
                if "PRICE_FILTER" not in f_map or "LOT_SIZE" not in f_map:
                    continue
                pf = f_map["PRICE_FILTER"]
                ls = f_map["LOT_SIZE"]
                mn = f_map.get("MIN_NOTIONAL", {})
                min_notional = float(mn["notional"]) if "notional" in mn else 5.0
                self.upsert_spec(
                    symbol=str(row["symbol"]),
                    tick_size=str(pf["tickSize"]),
                    step_size=str(ls["stepSize"]),
                    min_qty=str(ls["minQty"]),
                    min_notional_usd=min_notional,
                )
                loaded += 1
            return loaded
        # Поддержка плоского словаря {SYMBOL: {tick_size, step_size, min_qty, min_notional_usd}}
        for sym, raw in payload.items():
            if isinstance(raw, dict) and "tick_size" in raw:
                self.upsert_spec(
                    symbol=sym,
                    tick_size=raw["tick_size"],
                    step_size=raw["step_size"],
                    min_qty=raw["min_qty"],
                    min_notional_usd=float(raw["min_notional_usd"]),
                )
                loaded += 1
        return loaded

    def get_spec(self, symbol: str) -> SymbolSpec:
        sym_u = symbol.upper().strip()
        if sym_u not in self._specs:
            raise KeyError(f"Symbol spec not found for '{sym_u}' in specifications cache")
        return self._specs[sym_u]

    def upsert_spec(
        self,
        symbol: str,
        tick_size: str | float,
        step_size: str | float,
        min_qty: str | float,
        min_notional_usd: float,
    ) -> SymbolSpec:
        sym_u = str(symbol).upper().strip()
        spec = SymbolSpec(
            symbol=sym_u,
            tick_size=Decimal(str(tick_size)),
            step_size=Decimal(str(step_size)),
            min_qty=Decimal(str(min_qty)),
            min_notional_usd=float(min_notional_usd),
        )
        self._specs[sym_u] = spec
        return spec

    def round_price(self, symbol: str, price: float) -> float:
        spec = self.get_spec(symbol)
        d_price = Decimal(str(price))
        rounded = (d_price / spec.tick_size).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * spec.tick_size
        return float(rounded)

    def round_qty(self, symbol: str, qty: float) -> float:
        if qty <= 0.0:
            return 0.0
        spec = self.get_spec(symbol)
        d_qty = Decimal(str(qty))
        rounded = (d_qty / spec.step_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * spec.step_size
        if rounded < spec.min_qty:
            return 0.0
        return float(rounded)

    def check_min_notional(self, symbol: str, qty: float, price: float) -> bool:
        spec = self.get_spec(symbol)
        return (qty * price) >= spec.min_notional_usd
