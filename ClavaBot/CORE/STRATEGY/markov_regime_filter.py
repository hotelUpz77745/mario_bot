# Path: ClavaBot/CORE/STRATEGY/markov_regime_filter.py
# Role: Марковский фильтр рыночных режимов (3x3 матрица переходов Bull/Sideways/Bear без look-ahead)

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple
from ClavaBot.CORE.config_loader import MarkovRegimeConfig
from ClavaBot.CORE.models import Candle, SideType


_BULL = 0
_SIDEWAYS = 1
_BEAR = 2
_STATE_NAMES: Tuple[str, str, str] = ("BULL", "SIDEWAYS", "BEAR")


@dataclass(frozen=True, slots=True)
class MarkovDecision:
    """Результат оценки Марковского фильтра для потенциальной сделки."""
    allowed: bool
    size_multiplier: float
    current_state: str
    net_score_ratio: float
    persistence_ratio: float


def _matmul_3x3(a: List[List[float]], b: List[List[float]]) -> List[List[float]]:
    out = [[0.0, 0.0, 0.0] for _ in range(3)]
    for i in range(3):
        for j in range(3):
            out[i][j] = a[i][0] * b[0][j] + a[i][1] * b[1][j] + a[i][2] * b[2][j]
    return out


def _matpow_3x3(mat: List[List[float]], power: int) -> List[List[float]]:
    result = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    base = [row[:] for row in mat]
    exp = max(1, int(power))
    while exp > 0:
        if exp & 1:
            result = _matmul_3x3(result, base)
        exp >>= 1
        if exp > 0:
            base = _matmul_3x3(base, base)
    return result


class MarkovRegimeFilter:
    """
    Воссозданная модель режимов Маркова (3 состояния: BULL / SIDEWAYS / BEAR):
    - Дневные закрытия формируются по завершённым UTC-суткам (без заглядывания в будущее).
    - Режим дня определяется по доходности за return_lookback_days (по умолчанию 20 дней, +-5%).
    - Матрица переходов 3x3 строится исключительно по прошлым переходам (walk-forward).
    - Прогноз на forecast_horizon_days считается возведением матрицы переходов в степень k.
    """

    def __init__(self, cfg: MarkovRegimeConfig):
        self.cfg = cfg
        self._current_day_idx: Dict[str, int] = {}
        self._current_day_close: Dict[str, float] = {}
        self._daily_closes: Dict[str, List[float]] = {}
        self._daily_states: Dict[str, List[int]] = {}

    def update(self, symbol: str, candle: Candle) -> None:
        if not self.cfg.enabled:
            return

        day_idx = (candle.close_ts_ms - 1) // 86_400_000
        prev_day = self._current_day_idx.get(symbol)
        if prev_day is None:
            self._current_day_idx[symbol] = day_idx
            self._current_day_close[symbol] = candle.close
            self._daily_closes[symbol] = []
            self._daily_states[symbol] = []
            return

        if day_idx > prev_day:
            self._commit_completed_day(symbol, self._current_day_close[symbol])
            self._current_day_idx[symbol] = day_idx

        self._current_day_close[symbol] = candle.close

    def _commit_completed_day(self, symbol: str, close_px: float) -> None:
        closes = self._daily_closes[symbol]
        closes.append(close_px)
        lb = self.cfg.return_lookback_days
        if len(closes) <= lb:
            return

        prev_px = closes[-1 - lb]
        if prev_px <= 0.0:
            return
        ret_ratio = (close_px - prev_px) / prev_px

        if ret_ratio >= self.cfg.bull_return_threshold_ratio:
            state = _BULL
        elif ret_ratio <= -self.cfg.bear_return_threshold_ratio:
            state = _BEAR
        else:
            state = _SIDEWAYS

        self._daily_states[symbol].append(state)

    def _build_transition_matrix(self, states: List[int]) -> List[List[float]]:
        alpha = self.cfg.laplace_alpha
        counts = [[alpha, alpha, alpha] for _ in range(3)]

        win = self.cfg.transition_window_days
        seq = states[-win:] if (win is not None and len(states) > win) else states
        for i in range(len(seq) - 1):
            counts[seq[i]][seq[i + 1]] += 1.0

        probs: List[List[float]] = []
        for row in counts:
            total = row[0] + row[1] + row[2]
            probs.append([row[0] / total, row[1] / total, row[2] / total])
        return probs

    def evaluate(self, symbol: str, side: SideType) -> MarkovDecision:
        if not self.cfg.enabled:
            return MarkovDecision(
                allowed=True,
                size_multiplier=1.0,
                current_state="DISABLED",
                net_score_ratio=0.0,
                persistence_ratio=1.0,
            )

        states = self._daily_states.get(symbol, [])
        if len(states) < self.cfg.min_history_states:
            return MarkovDecision(
                allowed=True,
                size_multiplier=1.0,
                current_state="WARMING",
                net_score_ratio=0.0,
                persistence_ratio=0.0,
            )

        cur_state = states[-1]
        p_mat = self._build_transition_matrix(states)
        p_k = _matpow_3x3(p_mat, self.cfg.forecast_horizon_days)

        p_bull = p_k[cur_state][_BULL]
        p_bear = p_k[cur_state][_BEAR]
        net_score = p_bull - p_bear
        persistence = p_mat[cur_state][cur_state]
        state_name = _STATE_NAMES[cur_state]

        if side == "LONG":
            if self.cfg.block_long_in_bear and cur_state == _BEAR:
                return MarkovDecision(False, 0.0, state_name, net_score, persistence)
            if self.cfg.min_net_score_for_long_ratio is not None and net_score < self.cfg.min_net_score_for_long_ratio:
                return MarkovDecision(False, 0.0, state_name, net_score, persistence)
        else:
            if self.cfg.block_short_in_bull and cur_state == _BULL:
                return MarkovDecision(False, 0.0, state_name, net_score, persistence)
            if self.cfg.min_net_score_for_short_ratio is not None and (-net_score) < self.cfg.min_net_score_for_short_ratio:
                return MarkovDecision(False, 0.0, state_name, net_score, persistence)

        mult = self._compute_size_multiplier(cur_state, side, persistence)
        return MarkovDecision(
            allowed=True,
            size_multiplier=mult,
            current_state=state_name,
            net_score_ratio=net_score,
            persistence_ratio=persistence,
        )

    def _compute_size_multiplier(self, cur_state: int, side: SideType, persistence: float) -> float:
        if not self.cfg.dynamic_sizing_enabled:
            return 1.0
        if cur_state == _SIDEWAYS:
            return self.cfg.sideways_size_multiplier
        aligned = (cur_state == _BULL and side == "LONG") or (cur_state == _BEAR and side == "SHORT")
        if aligned:
            if persistence >= self.cfg.high_persistence_threshold_ratio:
                return self.cfg.aligned_high_persistence_multiplier
            return 1.0
        return self.cfg.counter_regime_size_multiplier
