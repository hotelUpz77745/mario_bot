# Path: MarioBot/CORE/DATA/feature_timeline.py
# Role: Полиморфная абстракция единой временной оси признаков (OHLCV + Funding + Индикаторы) для Backtest, Prod и Paper

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Optional, Sequence, Tuple

from MarioBot.CORE.models import Candle
from MarioBot.CORE.STRATEGY._indicators import (
    IncrementalADX,
    IncrementalATR,
    IncrementalEMA,
    RollingEfficiencyRatio,
)


@dataclass(frozen=True, slots=True)
class BarFeatureSnapshot:
    """Единый срез данных монеты на закрытии бара с общей меткой времени close_ts_ms."""
    symbol: str
    open_ts_ms: int
    close_ts_ms: int
    candle: Candle
    funding_rate: Optional[float]
    indicators: Dict[str, Optional[float]]


class BaseFeatureTimeline(ABC):
    """
    Универсальный полиморфный контракт временной оси признаков:
    - В Backtest реализуется через PrecompiledFeatureTimeline (O(1) поверх PrecompiledTimeGrid).
    - В Prod и Paper реализуется через LiveRingFeatureTimeline (кольцевой буфер с инкрементальным расчётом).
    """

    @abstractmethod
    def get_latest_snapshot(self, symbol: str) -> Optional[BarFeatureSnapshot]:
        raise NotImplementedError

    @abstractmethod
    def get_snapshot_at(self, symbol: str, close_ts_ms: int) -> Optional[BarFeatureSnapshot]:
        raise NotImplementedError

    @abstractmethod
    def get_latest_funding(self, symbol: str) -> Optional[float]:
        raise NotImplementedError

    def get_indicator(self, symbol: str, name: str) -> Optional[float]:
        snap = self.get_latest_snapshot(symbol)
        if snap is None:
            return None
        return snap.indicators.get(name)


class PrecompiledFeatureTimeline(BaseFeatureTimeline):
    """Адаптер бэктеста: отдаёт BarFeatureSnapshot за O(1) из единой прекомпилированной таблицы."""

    def __init__(self, grid: Any):
        self.grid = grid
        self._cursor_bar: int = -1
        self._close_ts_to_bar: Dict[int, int] = {
            int(ts): idx for idx, ts in enumerate(grid.bar_close_ts_ms.tolist())
        }
        self._last_funding_by_col: Dict[int, float] = {}

    def advance_to_bar(self, bar_idx: int) -> None:
        self._cursor_bar = int(bar_idx)
        if bool(self.grid.funding_any_bar[bar_idx]):
            for col in range(len(self.grid.symbols)):
                if bool(self.grid.funding_valid[bar_idx, col]):
                    self._last_funding_by_col[col] = float(self.grid.funding_rates[bar_idx, col])

    def _build_snapshot(self, symbol: str, bar_idx: int) -> Optional[BarFeatureSnapshot]:
        sym_u = symbol.upper().strip()
        col = self.grid.symbol_to_col.get(sym_u)
        if col is None or bar_idx < 0 or bar_idx >= int(self.grid.bar_close_ts_ms.shape[0]):
            return None
        if not bool(self.grid.valid[bar_idx, col]):
            return None
        row = self.grid.ohlcv[bar_idx, col]
        open_ts = int(self.grid.bar_open_ts_ms[bar_idx])
        close_ts = int(self.grid.bar_close_ts_ms[bar_idx])
        candle = Candle(
            symbol=sym_u,
            open_ts_ms=open_ts,
            close_ts_ms=close_ts,
            open=float(row[0]),
            high=float(row[1]),
            low=float(row[2]),
            close=float(row[3]),
            volume=float(row[4]),
        )
        ind_map = {name: self.grid.get_indicator(name, bar_idx, col) for name in self.grid.indicators}
        f_rate = self.grid.get_funding(bar_idx, col)
        if f_rate is None:
            f_rate = self._last_funding_by_col.get(col)
        return BarFeatureSnapshot(
            symbol=sym_u,
            open_ts_ms=open_ts,
            close_ts_ms=close_ts,
            candle=candle,
            funding_rate=f_rate,
            indicators=ind_map,
        )

    def get_latest_snapshot(self, symbol: str) -> Optional[BarFeatureSnapshot]:
        if self._cursor_bar < 0:
            return None
        return self._build_snapshot(symbol, self._cursor_bar)

    def get_snapshot_at(self, symbol: str, close_ts_ms: int) -> Optional[BarFeatureSnapshot]:
        b_idx = self._close_ts_to_bar.get(int(close_ts_ms))
        if b_idx is None:
            return None
        return self._build_snapshot(symbol, b_idx)

    def get_latest_funding(self, symbol: str) -> Optional[float]:
        col = self.grid.symbol_to_col.get(symbol.upper().strip())
        if col is None:
            return None
        return self._last_funding_by_col.get(col)


DEFAULT_LIVE_SPECS: Tuple[Tuple[str, str, int], ...] = (
    ("atr_14", "atr", 14),
    ("er_18", "er", 18),
    ("adx_14", "adx", 14),
    ("ema_50", "ema", 50),
)


class LiveRingFeatureTimeline(BaseFeatureTimeline):
    """
    Живая реализация единой временной оси для MarioBot (PROD) и MarioPaper (PAPER):
    - Хранит кольцевой буфер последних max_bars закрытых свечей (250д 4H = 1500 баров).
    - Синхронно обновляет индикаторы ядра и привязывает последний funding_rate к точному close_ts_ms.
    """

    def __init__(
        self,
        symbols: Sequence[str],
        max_bars: int = 1500,
        indicator_specs: Sequence[Tuple[str, str, int]] = DEFAULT_LIVE_SPECS,
    ):
        self.max_bars = max(10, int(max_bars))
        self._specs = tuple(indicator_specs)
        self._history: Dict[str, Deque[BarFeatureSnapshot]] = {}
        self._by_ts: Dict[str, Dict[int, BarFeatureSnapshot]] = {}
        self._trackers: Dict[str, Dict[str, Any]] = {}
        self._latest_funding: Dict[str, float] = {}
        for sym in symbols:
            self._ensure_symbol(sym)

    def _ensure_symbol(self, symbol: str) -> str:
        sym_u = symbol.upper().strip()
        if sym_u not in self._history:
            self._history[sym_u] = deque(maxlen=self.max_bars)
            self._by_ts[sym_u] = {}
            t_map: Dict[str, Any] = {}
            for name, kind, period in self._specs:
                if kind == "atr":
                    t_map[name] = IncrementalATR(period)
                elif kind == "er":
                    t_map[name] = RollingEfficiencyRatio(period)
                elif kind == "adx":
                    t_map[name] = IncrementalADX(period)
                elif kind == "ema":
                    t_map[name] = IncrementalEMA(period)
                else:
                    raise ValueError(f"Unsupported indicator kind: {kind}")
            self._trackers[sym_u] = t_map
        return sym_u

    def update_funding(self, symbol: str, funding_rate: float) -> None:
        sym_u = self._ensure_symbol(symbol)
        self._latest_funding[sym_u] = float(funding_rate)

    def push_candle(self, candle: Candle, funding_rate: Optional[float] = None) -> BarFeatureSnapshot:
        sym_u = self._ensure_symbol(candle.symbol)
        if funding_rate is not None:
            self._latest_funding[sym_u] = float(funding_rate)

        ind_vals: Dict[str, Optional[float]] = {}
        for name, kind, _period in self._specs:
            tr = self._trackers[sym_u][name]
            if kind in ("er", "ema"):
                ind_vals[name] = tr.update(candle.close)
            else:
                ind_vals[name] = tr.update(candle)

        snap = BarFeatureSnapshot(
            symbol=sym_u,
            open_ts_ms=candle.open_ts_ms,
            close_ts_ms=candle.close_ts_ms,
            candle=candle,
            funding_rate=self._latest_funding.get(sym_u),
            indicators=ind_vals,
        )
        dq = self._history[sym_u]
        if len(dq) == self.max_bars:
            dropped = dq[0]
            self._by_ts[sym_u].pop(dropped.close_ts_ms, None)
        dq.append(snap)
        self._by_ts[sym_u][snap.close_ts_ms] = snap
        return snap

    def get_latest_snapshot(self, symbol: str) -> Optional[BarFeatureSnapshot]:
        dq = self._history.get(symbol.upper().strip())
        if not dq:
            return None
        return dq[-1]

    def get_snapshot_at(self, symbol: str, close_ts_ms: int) -> Optional[BarFeatureSnapshot]:
        return self._by_ts.get(symbol.upper().strip(), {}).get(int(close_ts_ms))

    def get_latest_funding(self, symbol: str) -> Optional[float]:
        return self._latest_funding.get(symbol.upper().strip())
