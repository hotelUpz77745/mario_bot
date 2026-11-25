# Path: ClavaBacktest/ENGINE/time_grid_precompiler.py
# Role: Единая система прекомпиляции OHLCV, Funding Rates и индикаторов к единым меткам времени (O(1) доступ, 0 look-ahead)

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
import numpy as np

from ClavaBot.CORE.models import Candle
from ClavaBot.CORE.STRATEGY._indicators import (
    IncrementalADX,
    IncrementalATR,
    IncrementalEMA,
    RollingEfficiencyRatio,
)


@dataclass(frozen=True, slots=True)
class IndicatorSpec:
    """Описание индикатора для прекомпиляции на единую временную ось."""
    name: str
    kind: str  # "atr" | "er" | "adx" | "ema"
    period: int


DEFAULT_MARKOV_INDICATOR_SPECS: Tuple[IndicatorSpec, ...] = (
    IndicatorSpec("atr_14", "atr", 14),
    IndicatorSpec("er_18", "er", 18),
    IndicatorSpec("adx_14", "adx", 14),
    IndicatorSpec("ema_50", "ema", 50),
)


@dataclass
class PrecompiledTimeGrid:
    """
    Единая прекомпилированная таблица признаков, привязанная к общим меткам времени:
    - bar_open_ts_ms[b] / bar_close_ts_ms[b] — единый временной индекс бара b (0..B-1);
    - ohlcv[b, s, :] и valid[b, s] — свеча монеты s на баре b;
    - funding_rates[b, s] и funding_valid[b, s] — 8h funding, привязанный к тому же бару b;
    - indicators[name][b, s] — каузально посчитанный индикатор на закрытии бара b (NaN при прогреве).
    """

    symbols: List[str]
    tf_ms: int
    bar_open_ts_ms: np.ndarray            # (B,) int64
    bar_close_ts_ms: np.ndarray           # (B,) int64
    ohlcv: np.ndarray                     # (B, S, 5) float64
    valid: np.ndarray                     # (B, S) bool
    funding_rates: np.ndarray             # (B, S) float64
    funding_valid: np.ndarray             # (B, S) bool
    funding_any_bar: np.ndarray           # (B,) bool
    indicators: Dict[str, np.ndarray] = field(default_factory=dict)
    symbol_to_col: Dict[str, int] = field(init=False)

    def __post_init__(self) -> None:
        self.symbol_to_col = {s: i for i, s in enumerate(self.symbols)}

    def get_indicator(self, name: str, bar_idx: int, sym_col: int) -> Optional[float]:
        val = float(self.indicators[name][bar_idx, sym_col])
        return None if np.isnan(val) else val

    def get_funding(self, bar_idx: int, sym_col: int) -> Optional[float]:
        if not bool(self.funding_valid[bar_idx, sym_col]):
            return None
        return float(self.funding_rates[bar_idx, sym_col])

    def save_npz(self, path: Path) -> None:
        payload: Dict[str, Any] = {
            "symbols": np.asarray(self.symbols, dtype="U32"),
            "tf_ms": np.asarray([self.tf_ms], dtype=np.int64),
            "bar_open_ts_ms": self.bar_open_ts_ms,
            "bar_close_ts_ms": self.bar_close_ts_ms,
            "ohlcv": self.ohlcv,
            "valid": self.valid,
            "funding_rates": self.funding_rates,
            "funding_valid": self.funding_valid,
            "funding_any_bar": self.funding_any_bar,
            "indicator_names": np.asarray(sorted(self.indicators.keys()), dtype="U64"),
        }
        for name, arr in self.indicators.items():
            payload[f"ind__{name}"] = arr
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, **payload)

    @classmethod
    def load_npz(cls, path: Path) -> "PrecompiledTimeGrid":
        npz = np.load(path, allow_pickle=False)
        symbols = [str(s) for s in npz["symbols"].tolist()]
        tf_ms = int(npz["tf_ms"][0])
        ind_names = [str(n) for n in npz["indicator_names"].tolist()]
        indicators = {n: npz[f"ind__{n}"].astype(np.float64) for n in ind_names}
        return cls(
            symbols=symbols,
            tf_ms=tf_ms,
            bar_open_ts_ms=npz["bar_open_ts_ms"].astype(np.int64),
            bar_close_ts_ms=npz["bar_close_ts_ms"].astype(np.int64),
            ohlcv=npz["ohlcv"].astype(np.float64),
            valid=npz["valid"].astype(bool),
            funding_rates=npz["funding_rates"].astype(np.float64),
            funding_valid=npz["funding_valid"].astype(bool),
            funding_any_bar=npz["funding_any_bar"].astype(bool),
            indicators=indicators,
        )


class UnifiedTimeGridPrecompiler:
    """
    Компилирует сырые 1m свечи, 8h funding rates и индикаторы ядра на единую сетку меток времени.
    Гарантирует:
    1. Отсутствие look-ahead (индикаторы считаются строго инкрементально классами из ClavaBot.CORE.STRATEGY._indicators).
    2. Единую привязку по целочисленному индексу времени для OHLCV, Funding и всех индикаторов.
    3. O(1) доступ в симуляции без повторных np.searchsorted и повторных пересчётов при свипах.
    """

    @staticmethod
    def compile_funding_to_1m_grid(
        ts_1m_ms: np.ndarray,
        symbols: Sequence[str],
        raw_funding: Dict[str, Tuple[np.ndarray, np.ndarray]],
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Привязывает 8h funding rates к единой 1-минутной сетке ts_1m_ms (шаг 60_000 мс):
        - funding_rates: shape (T, S) float64 (0.0 вне моментов расчёта фандинга)
        - funding_mask: shape (T,) bool (True только на 1m барах, где есть хотя бы одно событие фандинга)
        """
        t_len = int(ts_1m_ms.shape[0])
        n_syms = len(symbols)
        rates_grid = np.zeros((t_len, n_syms), dtype=np.float64)
        mask_bar = np.zeros(t_len, dtype=bool)
        if t_len == 0 or n_syms == 0 or not raw_funding:
            return rates_grid, mask_bar

        t0 = int(ts_1m_ms[0])
        t_end = int(ts_1m_ms[-1]) + 60_000
        for col, sym in enumerate(symbols):
            pair = raw_funding.get(sym.upper().strip())
            if pair is None:
                continue
            f_ts, f_rt = pair
            if f_ts.shape[0] == 0:
                continue
            in_rng = (f_ts >= t0) & (f_ts < t_end)
            if not np.any(in_rng):
                continue
            sel_ts = f_ts[in_rng]
            sel_rt = f_rt[in_rng]
            # Округление вниз до минутного слота [t, t+60s): fundingTime 00:00:00.001 попадает ровно в минуту 00:00
            idxs = ((sel_ts - t0) // 60_000).astype(np.int64)
            ok = (idxs >= 0) & (idxs < t_len)
            idxs = idxs[ok]
            sel_rt = sel_rt[ok]
            if idxs.shape[0] > 0:
                rates_grid[idxs, col] = sel_rt
                mask_bar[idxs] = True
        return rates_grid, mask_bar

    @staticmethod
    def compile_tf_grid(
        ts_1m_ms: np.ndarray,
        ohlcv_1m: np.ndarray,
        valid_1m: np.ndarray,
        symbols: Sequence[str],
        tf_ms: int,
        raw_funding: Optional[Dict[str, Tuple[np.ndarray, np.ndarray]]] = None,
        indicator_specs: Sequence[IndicatorSpec] = DEFAULT_MARKOV_INDICATOR_SPECS,
    ) -> PrecompiledTimeGrid:
        """Собирает единую таблицу (B, S) для OHLCV, Funding Rates и индикаторов на общей оси времени tf_ms."""
        clean_syms = [s.upper().strip() for s in symbols]
        n_syms = len(clean_syms)
        n_1m = int(ts_1m_ms.shape[0])
        if n_1m == 0:
            empty_i = np.empty(0, dtype=np.int64)
            empty_b = np.empty(0, dtype=bool)
            return PrecompiledTimeGrid(
                symbols=clean_syms,
                tf_ms=tf_ms,
                bar_open_ts_ms=empty_i,
                bar_close_ts_ms=empty_i,
                ohlcv=np.zeros((0, n_syms, 5), dtype=np.float64),
                valid=np.zeros((0, n_syms), dtype=bool),
                funding_rates=np.zeros((0, n_syms), dtype=np.float64),
                funding_valid=np.zeros((0, n_syms), dtype=bool),
                funding_any_bar=empty_b,
                indicators={sp.name: np.zeros((0, n_syms), dtype=np.float64) for sp in indicator_specs},
            )

        bucket_ids = ts_1m_ms // tf_ms
        change_pts = np.flatnonzero(bucket_ids[1:] != bucket_ids[:-1]) + 1
        starts = np.concatenate(([0], change_pts))
        ends = np.concatenate((change_pts, [n_1m]))
        b_len = int(starts.shape[0])

        bar_open = (bucket_ids[starts] * tf_ms).astype(np.int64)
        bar_close = (bar_open + tf_ms).astype(np.int64)
        ohlcv_tf = np.zeros((b_len, n_syms, 5), dtype=np.float64)
        valid_tf = np.zeros((b_len, n_syms), dtype=bool)

        for b in range(b_len):
            s_i, e_i = int(starts[b]), int(ends[b])
            for col in range(n_syms):
                v_slice = valid_1m[s_i:e_i, col]
                if not np.any(v_slice):
                    continue
                sub = ohlcv_1m[s_i:e_i, col][v_slice]
                ohlcv_tf[b, col, 0] = sub[0, 0]
                ohlcv_tf[b, col, 1] = np.max(sub[:, 1])
                ohlcv_tf[b, col, 2] = np.min(sub[:, 2])
                ohlcv_tf[b, col, 3] = sub[-1, 3]
                ohlcv_tf[b, col, 4] = np.sum(sub[:, 4])
                valid_tf[b, col] = True

        # 1. Привязка Funding Rates к тем же индексам баров b (O(1) по единой оси bar_open)
        funding_tf = np.zeros((b_len, n_syms), dtype=np.float64)
        funding_valid = np.zeros((b_len, n_syms), dtype=bool)
        funding_any = np.zeros(b_len, dtype=bool)
        if raw_funding:
            first_bucket = int(bucket_ids[0])
            bucket_to_b = {int(bucket_ids[int(starts[b])]): b for b in range(b_len)}
            t_start = int(bar_open[0])
            t_end = int(bar_close[-1])
            for col, sym in enumerate(clean_syms):
                pair = raw_funding.get(sym)
                if pair is None or pair[0].shape[0] == 0:
                    continue
                f_ts, f_rt = pair
                in_rng = (f_ts >= t_start) & (f_ts < t_end)
                if not np.any(in_rng):
                    continue
                for ts_val, rt_val in zip(f_ts[in_rng].tolist(), f_rt[in_rng].tolist()):
                    b_id = int(ts_val) // tf_ms
                    b_idx = bucket_to_b.get(b_id)
                    if b_idx is not None:
                        funding_tf[b_idx, col] += float(rt_val)
                        funding_valid[b_idx, col] = True
                        funding_any[b_idx] = True
            _ = first_bucket

        # 2. Прекомпиляция всех индикаторов строго теми же инкрементальными классами ядра
        indicators: Dict[str, np.ndarray] = {}
        for spec in indicator_specs:
            mat = np.full((b_len, n_syms), np.nan, dtype=np.float64)
            for col, sym in enumerate(clean_syms):
                tracker = _build_indicator_tracker(spec)
                for b in range(b_len):
                    if not bool(valid_tf[b, col]):
                        continue
                    row = ohlcv_tf[b, col]
                    if spec.kind == "er":
                        val = tracker.update(float(row[3]))
                    elif spec.kind == "ema":
                        val = tracker.update(float(row[3]))
                    else:
                        candle = Candle(
                            symbol=sym,
                            open_ts_ms=int(bar_open[b]),
                            close_ts_ms=int(bar_close[b]),
                            open=float(row[0]),
                            high=float(row[1]),
                            low=float(row[2]),
                            close=float(row[3]),
                            volume=float(row[4]),
                        )
                        val = tracker.update(candle)
                    if val is not None:
                        mat[b, col] = float(val)
            indicators[spec.name] = mat

        return PrecompiledTimeGrid(
            symbols=clean_syms,
            tf_ms=tf_ms,
            bar_open_ts_ms=bar_open,
            bar_close_ts_ms=bar_close,
            ohlcv=ohlcv_tf,
            valid=valid_tf,
            funding_rates=funding_tf,
            funding_valid=funding_valid,
            funding_any_bar=funding_any,
            indicators=indicators,
        )


def _build_indicator_tracker(spec: IndicatorSpec):
    if spec.kind == "atr":
        return IncrementalATR(spec.period)
    if spec.kind == "er":
        return RollingEfficiencyRatio(spec.period)
    if spec.kind == "adx":
        return IncrementalADX(spec.period)
    if spec.kind == "ema":
        return IncrementalEMA(spec.period)
    raise ValueError(f"Unsupported indicator kind: {spec.kind}")
