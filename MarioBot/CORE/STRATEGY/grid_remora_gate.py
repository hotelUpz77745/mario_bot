# Path: MarioBot/CORE/STRATEGY/grid_remora_gate.py
# Role: Входной гейт «прилипалы» (проверка активного пула HRON_3 и порога усреднения)
#       без вмешательства в жизненный цикл и выходы уже открытых позиций Mario.

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from MarioBot.CORE.config_loader import RemoraGateConfig
from MarioBot.CORE.models import SideType


@dataclass(frozen=True, slots=True)
class CoinGridState:
    """Срез состояния сетки HRON_3 по одной монете."""
    symbol: str
    in_active_pool: bool
    long_level: int = -1
    short_level: int = -1
    long_adv_pct: float = 0.0
    short_adv_pct: float = 0.0
    updated_ts_ms: int = 0


class GridStateProvider(ABC):
    """Абстрактный провайдер состояния сетки внешнего сеточника (Backtest и Prod)."""

    @abstractmethod
    def get_snapshot(self, now_ms: int) -> Optional[Dict[str, CoinGridState]]:
        raise NotImplementedError


class InMemoryGridStateProvider(GridStateProvider):
    """In-memory провайдер для unit-тестов и синхронизированного бэктеста."""

    def __init__(self, states: Optional[Dict[str, CoinGridState]] = None):
        self._states: Dict[str, CoinGridState] = dict(states) if states else {}

    def set_snapshot(self, states: Dict[str, CoinGridState]) -> None:
        self._states = {k.upper(): v for k, v in states.items()}

    def update_coin(self, state: CoinGridState) -> None:
        self._states[state.symbol.upper()] = state

    def get_snapshot(self, now_ms: int) -> Optional[Dict[str, CoinGridState]]:
        return self._states


class JsonSnapshotGridStateProvider(GridStateProvider):
    """Read-only провайдер, читающий runtime JSON-снапшот состояния сетки HRON_3."""

    def __init__(self, snapshot_path: Path):
        self.snapshot_path = Path(snapshot_path)

    def get_snapshot(self, now_ms: int) -> Optional[Dict[str, CoinGridState]]:
        if not self.snapshot_path.exists():
            return None
        try:
            raw = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None

        ts_ms = int(raw.get("updated_ts_ms") or 0)
        coins = raw.get("symbols")
        if not isinstance(coins, dict):
            return None

        out: Dict[str, CoinGridState] = {}
        for sym, row in coins.items():
            if not isinstance(row, dict):
                continue
            sym_u = str(sym).upper().strip()
            out[sym_u] = CoinGridState(
                symbol=sym_u,
                in_active_pool=bool(row.get("in_active_pool", False)),
                long_level=int(row.get("long_level", -1)),
                short_level=int(row.get("short_level", -1)),
                long_adv_pct=float(row.get("long_adv_pct", 0.0)),
                short_adv_pct=float(row.get("short_adv_pct", 0.0)),
                updated_ts_ms=int(row.get("updated_ts_ms", ts_ms)),
            )
        return out


class GridRemoraGate:
    """
    Гейт новых входов для гибрида HRON_3 + Mario:
    - Проверяет активность монеты в текущем пуле HRON_3 и пробой порога усреднения X.
    - Никак не ограничивает ведение, добор и закрытие уже открытых позиций Mario.
    """

    def __init__(self, cfg: RemoraGateConfig, provider: Optional[GridStateProvider] = None):
        self.cfg = cfg
        if provider is not None:
            self.provider: Optional[GridStateProvider] = provider
        elif cfg.enabled and cfg.snapshot_path is not None:
            self.provider = JsonSnapshotGridStateProvider(Path(cfg.snapshot_path))
        else:
            self.provider = None

    def set_provider(self, provider: Optional[GridStateProvider]) -> None:
        self.provider = provider

    def _fresh_snapshot(self, now_ms: int) -> Optional[Dict[str, CoinGridState]]:
        if self.provider is None:
            return None
        snap = self.provider.get_snapshot(now_ms)
        if not snap:
            return None
        return snap

    def _is_fresh(self, st: CoinGridState, now_ms: int) -> bool:
        if self.cfg.max_staleness_ms <= 0:
            return True
        if st.updated_ts_ms <= 0:
            return False
        return (now_ms - st.updated_ts_ms) <= self.cfg.max_staleness_ms

    def _meets_threshold(self, st: CoinGridState, side: SideType) -> bool:
        if self.cfg.mode == "counter_hedge":
            lvl = st.short_level if side == "LONG" else st.long_level
            adv = st.short_adv_pct if side == "LONG" else st.long_adv_pct
        else:
            lvl = max(st.long_level, st.short_level)
            adv = max(st.long_adv_pct, st.short_adv_pct)

        if lvl < self.cfg.min_grid_level:
            return False
        if self.cfg.min_adv_pct is not None and adv < self.cfg.min_adv_pct:
            return False
        return True

    def is_in_active_pool(self, symbol: str, now_ms: int) -> bool:
        if not self.cfg.enabled:
            return True
        snap = self._fresh_snapshot(now_ms)
        if not snap:
            return False
        st = snap.get(symbol.upper())
        if st is None or not self._is_fresh(st, now_ms):
            return False
        return st.in_active_pool

    def can_open_entry(self, symbol: str, side: SideType, now_ms: int) -> bool:
        if not self.cfg.enabled:
            return True
        snap = self._fresh_snapshot(now_ms)
        if not snap:
            return False
        st = snap.get(symbol.upper())
        if st is None or not self._is_fresh(st, now_ms):
            return False
        if not st.in_active_pool:
            return False
        return self._meets_threshold(st, side)

    def get_active_pool_symbols(self, now_ms: int) -> list[str]:
        if not self.cfg.enabled:
            return []
        snap = self._fresh_snapshot(now_ms)
        if not snap:
            return []
        return sorted(
            sym for sym, st in snap.items()
            if st.in_active_pool and self._is_fresh(st, now_ms)
        )

    def count_triggered_coins(self, now_ms: int) -> int:
        if not self.cfg.enabled:
            return 0
        snap = self._fresh_snapshot(now_ms)
        if not snap:
            return 0
        cnt = 0
        for st in snap.values():
            if not st.in_active_pool or not self._is_fresh(st, now_ms):
                continue
            if self._meets_threshold(st, "LONG") or self._meets_threshold(st, "SHORT"):
                cnt += 1
        return cnt
