# Path: ClavaBot/CORE/spec_manager.py
# Role: Менеджер спецификаций инструментов (tickSize, stepSize, minQty, minNotional)

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Dict


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional_usd: float


class SpecManager:
    """
    Округляет цены и объёмы строго по спецификациям биржи (без сетевых вызовов в ядре).
    """

    def __init__(self, specs_cfg: Dict[str, Dict[str, Any]]):
        self._specs: Dict[str, SymbolSpec] = {}
        for sym, raw in specs_cfg.items():
            sym_u = str(sym).upper().strip()
            self._specs[sym_u] = SymbolSpec(
                symbol=sym_u,
                tick_size=Decimal(str(raw["tick_size"])),
                step_size=Decimal(str(raw["step_size"])),
                min_qty=Decimal(str(raw["min_qty"])),
                min_notional_usd=float(raw["min_notional_usd"]),
            )

    def get_spec(self, symbol: str) -> SymbolSpec:
        sym_u = symbol.upper().strip()
        if sym_u not in self._specs:
            raise KeyError(f"Symbol spec not found for '{sym_u}' in CFG/app.json")
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
