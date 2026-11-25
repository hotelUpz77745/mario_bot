# Path: ClavaBot/CORE/STRATEGY/pool_selector.py
# Role: Каузальный Walk-Forward селектор торгового пула монет (зрелость, ликвидность, трендовость без look-ahead)

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple
import numpy as np


@dataclass(frozen=True, slots=True)
class PoolSelectorConfig:
    """Параметры строго каузального walk-forward отбора пула монет."""
    rebalance_interval_days: int
    min_history_days: int
    lookback_days: int
    target_pool_size: int
    min_median_atr_pct_ratio: float
    min_avg_daily_quote_vol_usd: float


@dataclass(frozen=True, slots=True)
class CoinPoolScore:
    symbol: str
    history_days: int
    avg_daily_quote_vol_usd: float
    median_atr_pct_ratio: float
    avg_efficiency_ratio: float
    composite_score: float


class WalkForwardPoolSelector:
    """
    Накапливает дневные OHLCV-сводки по каждой монете вселенной (строго из прошлого)
    и на границах ребалансировки (раз в 90/180 дней) отбирает топ-K монет без look-ahead:
    1. Возраст истории >= min_history_days (отсекает сырые листинги до созревания GMM).
    2. Среднедневной оборот >= min_avg_daily_quote_vol_usd (ликвидность).
    3. Медианный дневной (High-Low)/Close >= min_median_atr_pct_ratio (отсекает сверхвялые активы).
    4. Ранжирование по composite_score = avg_ER * min(median_atr_pct, 0.12) * log1p(vol).
    """

    def __init__(self, cfg: PoolSelectorConfig, universe: Sequence[str]):
        self.cfg = cfg
        self.universe: List[str] = [s.upper().strip() for s in universe]
        self._daily_close: Dict[str, List[float]] = {s: [] for s in self.universe}
        self._daily_range_ratio: Dict[str, List[float]] = {s: [] for s in self.universe}
        self._daily_quote_vol: Dict[str, List[float]] = {s: [] for s in self.universe}

        self._cur_day: Dict[str, int] = {}
        self._acc_h: Dict[str, float] = {}
        self._acc_l: Dict[str, float] = {}
        self._acc_c: Dict[str, float] = {}
        self._acc_qvol: Dict[str, float] = {}

        self._last_rebalance_day: int = -1
        self.active_pool: List[str] = []

    def update_bar(
        self,
        symbol: str,
        close_ts_ms: int,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> None:
        sym = symbol.upper().strip()
        if sym not in self._daily_close or close <= 0.0:
            return
        day_idx = (close_ts_ms - 1) // 86_400_000
        prev = self._cur_day.get(sym)
        if prev is None:
            self._cur_day[sym] = day_idx
            self._acc_h[sym] = high
            self._acc_l[sym] = low
            self._acc_c[sym] = close
            self._acc_qvol[sym] = volume * close
            return
        if day_idx > prev:
            self._commit_day(sym)
            self._cur_day[sym] = day_idx
            self._acc_h[sym] = high
            self._acc_l[sym] = low
            self._acc_c[sym] = close
            self._acc_qvol[sym] = volume * close
        else:
            self._acc_h[sym] = max(self._acc_h[sym], high)
            self._acc_l[sym] = min(self._acc_l[sym], low)
            self._acc_c[sym] = close
            self._acc_qvol[sym] += volume * close

    def _commit_day(self, sym: str) -> None:
        c = self._acc_c[sym]
        if c <= 0.0:
            return
        rng = max(0.0, (self._acc_h[sym] - self._acc_l[sym]) / c)
        self._daily_close[sym].append(c)
        self._daily_range_ratio[sym].append(rng)
        self._daily_quote_vol[sym].append(self._acc_qvol[sym])

    def maybe_rebalance(self, ts_ms: int) -> Tuple[bool, List[str], List[CoinPoolScore]]:
        day_idx = (ts_ms - 1) // 86_400_000
        if self._last_rebalance_day >= 0 and (day_idx - self._last_rebalance_day) < self.cfg.rebalance_interval_days:
            return False, list(self.active_pool), []

        scores = self.score_universe()
        if not scores:
            return False, list(self.active_pool), []

        self._last_rebalance_day = day_idx
        self.active_pool = [s.symbol for s in scores[: self.cfg.target_pool_size]]
        return True, list(self.active_pool), scores

    def score_universe(self) -> List[CoinPoolScore]:
        lb = max(20, self.cfg.lookback_days)
        scored: List[CoinPoolScore] = []
        for sym in self.universe:
            closes = self._daily_close[sym]
            n_days = len(closes)
            if n_days < self.cfg.min_history_days:
                continue
            qvols = self._daily_quote_vol[sym][-min(30, n_days) :]
            avg_qvol = float(np.mean(qvols)) if qvols else 0.0
            if avg_qvol < self.cfg.min_avg_daily_quote_vol_usd:
                continue

            rngs = self._daily_range_ratio[sym][-min(lb, n_days) :]
            med_atr_pct = float(np.median(rngs)) if rngs else 0.0
            if med_atr_pct < self.cfg.min_median_atr_pct_ratio:
                continue

            # Скользящий 14-дневный Kaufman ER по дневным закрытиям за lookback_days
            tail_c = np.asarray(closes[-min(lb, n_days) :], dtype=np.float64)
            er_win = 14
            er_vals: List[float] = []
            if tail_c.shape[0] > er_win:
                diffs = np.abs(np.diff(tail_c))
                for i in range(er_win, tail_c.shape[0]):
                    denom = float(np.sum(diffs[i - er_win : i]))
                    if denom > 0.0:
                        er_vals.append(abs(float(tail_c[i] - tail_c[i - er_win])) / denom)
            avg_er = float(np.mean(er_vals)) if er_vals else 0.0

            # Предпочитаем стабильно трендовые, достаточно волатильные и ликвидные альты
            capped_vol_factor = min(med_atr_pct, 0.12)
            liq_factor = float(np.log1p(avg_qvol / 1_000_000.0))
            comp = avg_er * capped_vol_factor * max(1.0, liq_factor)
            scored.append(
                CoinPoolScore(
                    symbol=sym,
                    history_days=n_days,
                    avg_daily_quote_vol_usd=avg_qvol,
                    median_atr_pct_ratio=med_atr_pct,
                    avg_efficiency_ratio=avg_er,
                    composite_score=comp,
                )
            )
        scored.sort(key=lambda x: x.composite_score, reverse=True)
        return scored
