# Path: MarioBot/CORE/STRATEGY/markov_pure_signal_plugin.py
# Role: Чистый сигнальный плагин Маркова 3x3 с апгрейдами из notes2.md и умными широкими SL

from __future__ import annotations

from typing import Dict, List, Optional, Tuple
from MarioBot.CORE.config_loader import StrategyPassport
from MarioBot.CORE.models import Candle, ExitReasonType, OpenPosition, SideType, SignalEvent
from MarioBot.CORE.STRATEGY._indicators import (
    IncrementalATR,
    RollingEfficiencyRatio,
    calc_atr_volatility_multiplier,
    calc_wide_atr_sl_extreme,
)
from MarioBot.CORE.STRATEGY._markov_thresholds import AdaptiveThresholdFinder
from MarioBot.CORE.STRATEGY.base_signal_plugin import BaseSignalPlugin
from MarioBot.CORE.STRATEGY.markov_regime_filter import (
    MarkovDecision,
    MarkovRegimeFilter,
    _BEAR,
    _BULL,
    _SIDEWAYS,
    _STATE_NAMES,
    _matpow_3x3,
)


class _AdaptiveMarkovTracker:
    """Вспомогательный трекер walk-forward доходностей и матрицы с адаптивными порогами (GMM / Quantile)."""

    def __init__(
        self,
        lookback_days: int,
        horizon_days: int,
        transition_window_days: Optional[int],
        min_history_states: int,
        laplace_alpha: float,
        threshold_mode: str,
        calibration_window_days: int,
        recalibrate_interval_days: int,
    ):
        self.lookback_days = lookback_days
        self.horizon_days = horizon_days
        self.transition_window_days = transition_window_days
        self.min_history_states = min_history_states
        self.laplace_alpha = laplace_alpha
        self.threshold_mode = threshold_mode
        self.calibration_window_days = calibration_window_days
        self.recalibrate_interval_days = max(1, recalibrate_interval_days)

        self._cur_day: Dict[str, int] = {}
        self._cur_close: Dict[str, float] = {}
        self._closes: Dict[str, List[float]] = {}
        self._returns: Dict[str, List[float]] = {}
        self._cached_thresh: Dict[str, Tuple[float, float]] = {}

    def update(self, symbol: str, candle: Candle) -> None:
        day_idx = (candle.close_ts_ms - 1) // 86_400_000
        prev_day = self._cur_day.get(symbol)
        if prev_day is None:
            self._cur_day[symbol] = day_idx
            self._cur_close[symbol] = candle.close
            self._closes[symbol] = []
            self._returns[symbol] = []
            return
        if day_idx > prev_day:
            self._commit_day(symbol, self._cur_close[symbol])
            self._cur_day[symbol] = day_idx
        self._cur_close[symbol] = candle.close

    def _commit_day(self, symbol: str, close_px: float) -> None:
        closes = self._closes[symbol]
        closes.append(close_px)
        if len(closes) <= self.lookback_days:
            return
        prev_px = closes[-1 - self.lookback_days]
        if prev_px <= 0.0:
            return
        rets = self._returns[symbol]
        rets.append((close_px - prev_px) / prev_px)

        if symbol not in self._cached_thresh or (len(rets) % self.recalibrate_interval_days == 0):
            tail = rets[-self.calibration_window_days :]
            if self.threshold_mode == "gmm_3state":
                self._cached_thresh[symbol] = AdaptiveThresholdFinder.find_gmm_thresholds(tail)
            else:
                self._cached_thresh[symbol] = AdaptiveThresholdFinder.find_quantile_thresholds(tail)

    def evaluate(self, symbol: str) -> MarkovDecision:
        rets = self._returns.get(symbol, [])
        if len(rets) < self.min_history_states:
            return MarkovDecision(True, 1.0, "WARMING", 0.0, 0.0)

        bull_t, bear_t = self._cached_thresh.get(symbol, (0.05, 0.05))
        seq_rets = rets[-self.transition_window_days :] if (self.transition_window_days and len(rets) > self.transition_window_days) else rets
        states = [_BULL if r >= bull_t else (_BEAR if r <= -bear_t else _SIDEWAYS) for r in seq_rets]

        counts = [[self.laplace_alpha] * 3 for _ in range(3)]
        for i in range(len(states) - 1):
            counts[states[i]][states[i + 1]] += 1.0

        p_mat = [[c / sum(row) for c in row] for row in counts]
        p_k = _matpow_3x3(p_mat, self.horizon_days)
        cur_s = states[-1]
        net_score = p_k[cur_s][_BULL] - p_k[cur_s][_BEAR]
        persistence = p_mat[cur_s][cur_s]
        return MarkovDecision(True, 1.0, _STATE_NAMES[cur_s], net_score, persistence)


class MarkovPureSignalPlugin(BaseSignalPlugin):
    """
    Чистая марковская стратегия (notes2.md) с подключаемыми апгрейдами:
    - threshold_mode: 'fixed' | 'gmm_3state' | 'quantile'
    - dual_confirm_gmm: двойное подтверждение фиксированной и адаптивной GMM-матриц
    - confidence_sizing_enabled: сайзинг по |P(Bull)-P(Bear)| * stickiness
    - sl_mode: 'none' | 'pct_from_entry' | 'wide_atr' | 'chandelier_atr' | 'breakeven_atr'
    - reentry_mode: 'always' | 'on_flip_only'
    """

    def __init__(self, passport: StrategyPassport):
        params = passport.signal.params
        self.min_edge_ratio = float(params["min_edge_ratio"])
        self.threshold_mode = str(params["threshold_mode"])
        self.dual_confirm_gmm = bool(params["dual_confirm_gmm"])
        self.reentry_mode = str(params["reentry_mode"])
        self.sl_mode = str(params["sl_mode"])
        self.sl_pct_ratio = float(params["sl_pct_ratio"]) if params["sl_pct_ratio"] is not None else None
        self.atr_period = int(params["atr_period"])
        self.atr_sl_mult = float(params["atr_sl_mult"]) if params["atr_sl_mult"] is not None else None
        self.chandelier_atr_mult = (
            float(params["chandelier_atr_mult"]) if params["chandelier_atr_mult"] is not None else None
        )
        self.be_trigger_atr_mult = (
            float(params["be_trigger_atr_mult"]) if params["be_trigger_atr_mult"] is not None else None
        )
        self.confidence_sizing_enabled = bool(params["confidence_sizing_enabled"])
        self.confidence_scale = float(params["confidence_scale"])
        self.min_size_multiplier = float(params["min_size_multiplier"])
        self.max_size_multiplier = float(params["max_size_multiplier"])
        self.target_atr_pct_ratio = (
            float(params["target_atr_pct_ratio"]) if params["target_atr_pct_ratio"] is not None else None
        )
        self.vol_min_multiplier = float(params["vol_min_multiplier"])
        self.vol_max_multiplier = float(params["vol_max_multiplier"])
        self.block_in_sideways = bool(params["block_in_sideways"])
        self.exit_on_sideways = bool(params["exit_on_sideways"])
        self.er_period = int(params["er_period"])
        self.min_efficiency_ratio = (
            float(params["min_efficiency_ratio"]) if params["min_efficiency_ratio"] is not None else None
        )
        self.max_hold_days = int(params["max_hold_days"]) if params["max_hold_days"] is not None else None
        self.stale_exit_days = int(params["stale_exit_days"]) if params["stale_exit_days"] is not None else None
        self.stale_min_profit_atr_mult = (
            float(params["stale_min_profit_atr_mult"]) if params["stale_min_profit_atr_mult"] is not None else None
        )
        self.profit_trail_activation_atr_mult = (
            float(params["profit_trail_activation_atr_mult"])
            if params["profit_trail_activation_atr_mult"] is not None
            else None
        )
        self.profit_trail_distance_atr_mult = (
            float(params["profit_trail_distance_atr_mult"])
            if params["profit_trail_distance_atr_mult"] is not None
            else None
        )
        self.profit_trail_mode = str(params.get("profit_trail_mode", "4h_close"))

        self._markov_fixed = MarkovRegimeFilter(passport.markov_regime)
        self._markov_adaptive: Optional[_AdaptiveMarkovTracker] = None
        if self.threshold_mode in ("gmm_3state", "quantile") or self.dual_confirm_gmm:
            cal_win = int(params["calibration_window_days"])
            recal_days = int(params["recalibrate_interval_days"])
            mode = self.threshold_mode if self.threshold_mode in ("gmm_3state", "quantile") else "gmm_3state"
            self._markov_adaptive = _AdaptiveMarkovTracker(
                lookback_days=passport.markov_regime.return_lookback_days,
                horizon_days=passport.markov_regime.forecast_horizon_days,
                transition_window_days=passport.markov_regime.transition_window_days,
                min_history_states=passport.markov_regime.min_history_states,
                laplace_alpha=passport.markov_regime.laplace_alpha,
                threshold_mode=mode,
                calibration_window_days=cal_win,
                recalibrate_interval_days=recal_days,
            )

        self._atrs: Dict[str, IncrementalATR] = {sym: IncrementalATR(self.atr_period) for sym in passport.symbols}
        self._ers: Dict[str, RollingEfficiencyRatio] = {
            sym: RollingEfficiencyRatio(self.er_period) for sym in passport.symbols
        }
        self._last_day_idx: Dict[str, int] = {}
        self._daily_state_snapshot: Dict[str, Tuple[int, str, Optional[SideType]]] = {}
        self._last_emitted_side: Dict[str, Optional[SideType]] = {}
        self._trail_state: Dict[str, Tuple[int, float]] = {}
        self._profit_trail_state: Dict[str, Tuple[int, float, bool]] = {}
        self._be_armed: Dict[str, Tuple[int, bool]] = {}

    def update_and_evaluate(self, symbol: str, candle: Candle) -> Optional[SignalEvent]:
        self._markov_fixed.update(symbol, candle)
        if self._markov_adaptive is not None:
            self._markov_adaptive.update(symbol, candle)
        atr = self._atrs[symbol].update(candle)
        er = self._ers[symbol].update(candle.close)

        day_idx = (candle.close_ts_ms - 1) // 86_400_000
        if self._last_day_idx.get(symbol) == day_idx:
            return None
        self._last_day_idx[symbol] = day_idx

        dec_fixed = self._markov_fixed.evaluate(symbol, "LONG")
        if dec_fixed.current_state in ("DISABLED", "WARMING"):
            return None

        dec_main = dec_fixed
        if self.threshold_mode in ("gmm_3state", "quantile") and self._markov_adaptive is not None:
            dec_main = self._markov_adaptive.evaluate(symbol)
            if dec_main.current_state == "WARMING":
                return None

        raw_side = self._side_from_edge(dec_main.net_score_ratio)
        if raw_side is not None and self.block_in_sideways and dec_main.current_state == "SIDEWAYS":
            raw_side = None
        if raw_side is not None and self.min_efficiency_ratio is not None and er is not None and er < self.min_efficiency_ratio:
            raw_side = None
        if raw_side is not None and self.dual_confirm_gmm and self._markov_adaptive is not None:
            dec_gmm = self._markov_adaptive.evaluate(symbol)
            if dec_gmm.current_state == "WARMING" or self._side_from_edge(dec_gmm.net_score_ratio) != raw_side:
                raw_side = None

        self._daily_state_snapshot[symbol] = (day_idx, dec_main.current_state, raw_side)

        if self.reentry_mode == "on_flip_only":
            if raw_side is None:
                self._last_emitted_side[symbol] = None
                return None
            if raw_side == self._last_emitted_side.get(symbol):
                return None
            self._last_emitted_side[symbol] = raw_side
        elif raw_side is None:
            return None

        origin_extreme = self._compute_sl_extreme(raw_side, candle.close, atr)
        if self.sl_mode != "none" and origin_extreme is None:
            return None

        size_mult = 1.0
        if self.confidence_sizing_enabled:
            raw_conf = abs(dec_main.net_score_ratio) * dec_main.persistence_ratio * self.confidence_scale
            size_mult = max(self.min_size_multiplier, min(self.max_size_multiplier, raw_conf))
        size_mult *= calc_atr_volatility_multiplier(
            atr,
            candle.close,
            self.target_atr_pct_ratio,
            self.vol_min_multiplier,
            self.vol_max_multiplier,
        )

        return SignalEvent(
            symbol=symbol,
            side=raw_side,
            signal_ts_ms=candle.close_ts_ms,
            entry_ref_price=candle.close,
            origin_extreme_price=origin_extreme,
            size_multiplier=size_mult,
        )

    def get_atr(self, symbol: str) -> Optional[float]:
        """Возвращает текущее значение 4H ATR(14) по символу (для внутрисвечного/тикового трейлинга)."""
        tracker = self._atrs.get(symbol)
        return tracker.value if tracker is not None else None

    def _side_from_edge(self, net_score: float) -> Optional[SideType]:
        if net_score >= self.min_edge_ratio:
            return "LONG"
        if (-net_score) >= self.min_edge_ratio:
            return "SHORT"
        return None

    def _compute_sl_extreme(self, side: SideType, price: float, atr: Optional[float]) -> Optional[float]:
        if self.sl_mode == "none":
            return None
        if self.sl_mode == "pct_from_entry":
            if self.sl_pct_ratio is None or self.sl_pct_ratio <= 0.0:
                return None
            return max(1e-9, price * (1.0 - self.sl_pct_ratio)) if side == "LONG" else price * (1.0 + self.sl_pct_ratio)

        return calc_wide_atr_sl_extreme(side, price, atr, self.atr_sl_mult)

    def check_exit(self, symbol: str, candle: Candle, open_pos: OpenPosition) -> Optional[ExitReasonType]:
        hold_ms = candle.close_ts_ms - open_pos.entry_ts_ms
        if self.max_hold_days is not None and hold_ms >= (self.max_hold_days * 86_400_000):
            return "SIGNAL_EXIT"

        if self.exit_on_sideways:
            snap = self._daily_state_snapshot.get(symbol)
            if snap is not None:
                snap_day, cur_state, daily_side = snap
                cur_day = (candle.close_ts_ms - 1) // 86_400_000
                if snap_day == cur_day and cur_state == "SIDEWAYS":
                    if daily_side is None or daily_side == open_pos.side:
                        return "SIGNAL_EXIT"

        atr = self._atrs[symbol].value
        if atr is None or atr <= 0.0:
            return None

        # Умный Stale Drift Time-Stop: закрывает зависшие в дрейфе/минусе сделки после stale_exit_days
        if (
            self.stale_exit_days is not None
            and self.stale_min_profit_atr_mult is not None
            and hold_ms >= (self.stale_exit_days * 86_400_000)
        ):
            move_atr = (
                (candle.close - open_pos.entry_price) / atr
                if open_pos.side == "LONG"
                else (open_pos.entry_price - candle.close) / atr
            )
            if move_atr < self.stale_min_profit_atr_mult:
                return "SIGNAL_EXIT"

        # Активация трейлинга только ПОСЛЕ достижения профита profit_trail_activation_atr_mult * ATR (в режиме 4h_close)
        if (
            self.profit_trail_mode == "4h_close"
            and self.profit_trail_activation_atr_mult is not None
            and self.profit_trail_distance_atr_mult is not None
        ):
            prev_ts, prev_best, armed = self._profit_trail_state.get(symbol, (-1, open_pos.entry_price, False))
            if prev_ts != open_pos.entry_ts_ms:
                prev_best, armed = open_pos.entry_price, False
            act_dist = self.profit_trail_activation_atr_mult * atr
            trail_dist = self.profit_trail_distance_atr_mult * atr
            if open_pos.side == "LONG":
                best = max(prev_best, candle.high)
                if not armed and best >= (open_pos.entry_price + act_dist):
                    armed = True
                self._profit_trail_state[symbol] = (open_pos.entry_ts_ms, best, armed)
                if armed and candle.close <= (best - trail_dist):
                    return "SIGNAL_EXIT"
            else:
                best = min(prev_best, candle.low)
                if not armed and best <= (open_pos.entry_price - act_dist):
                    armed = True
                self._profit_trail_state[symbol] = (open_pos.entry_ts_ms, best, armed)
                if armed and candle.close >= (best + trail_dist):
                    return "SIGNAL_EXIT"

        if self.sl_mode == "chandelier_atr" and self.chandelier_atr_mult is not None:
            prev_ts, prev_best = self._trail_state.get(symbol, (-1, open_pos.entry_price))
            if prev_ts != open_pos.entry_ts_ms:
                prev_best = open_pos.entry_price
            if open_pos.side == "LONG":
                best = max(prev_best, candle.high)
                self._trail_state[symbol] = (open_pos.entry_ts_ms, best)
                if candle.close < (best - self.chandelier_atr_mult * atr):
                    return "SIGNAL_EXIT"
            else:
                best = min(prev_best, candle.low)
                self._trail_state[symbol] = (open_pos.entry_ts_ms, best)
                if candle.close > (best + self.chandelier_atr_mult * atr):
                    return "SIGNAL_EXIT"

        elif self.sl_mode == "breakeven_atr" and self.be_trigger_atr_mult is not None:
            prev_ts, armed = self._be_armed.get(symbol, (-1, False))
            if prev_ts != open_pos.entry_ts_ms:
                armed = False
            trig_dist = self.be_trigger_atr_mult * atr
            if open_pos.side == "LONG":
                if not armed and candle.high >= (open_pos.entry_price + trig_dist):
                    armed = True
                self._be_armed[symbol] = (open_pos.entry_ts_ms, armed)
                if armed and candle.close <= open_pos.entry_price * 1.0012:
                    return "SIGNAL_EXIT"
            else:
                if not armed and candle.low <= (open_pos.entry_price - trig_dist):
                    armed = True
                self._be_armed[symbol] = (open_pos.entry_ts_ms, armed)
                if armed and candle.close >= open_pos.entry_price * 0.9988:
                    return "SIGNAL_EXIT"

        return None
