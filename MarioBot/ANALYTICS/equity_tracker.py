# Path: MarioBot/ANALYTICS/equity_tracker.py
# Role: Трекер кривой капитала, исторических пиков/просадок и дневных доходностей

from __future__ import annotations

from typing import List, Tuple


class EquityTracker:
    """
    Отслеживает пик капитала, минимальное дно, максимальную просадку ($ и %)
    и формирует ряд дневных доходностей для расчёта Sharpe Ratio.
    """

    def __init__(self, start_balance_usd: float):
        self.start_balance_usd: float = float(start_balance_usd)
        self.peak_equity_usd: float = float(start_balance_usd)
        self.min_equity_usd: float = float(start_balance_usd)
        self.max_drawdown_usd: float = 0.0
        self.max_drawdown_pct: float = 0.0

        self._last_day_idx: int = -1
        self._last_day_equity: float = float(start_balance_usd)
        self.daily_returns: List[float] = []
        self.equity_curve: List[Tuple[int, float]] = []

    def record(self, ts_ms: int, equity_usd: float, record_curve_point: bool = False) -> None:
        if equity_usd > self.peak_equity_usd:
            self.peak_equity_usd = equity_usd
        if equity_usd < self.min_equity_usd:
            self.min_equity_usd = equity_usd

        dd_usd = self.peak_equity_usd - equity_usd
        if dd_usd > self.max_drawdown_usd:
            self.max_drawdown_usd = dd_usd

        if self.peak_equity_usd > 0.0:
            dd_pct = (dd_usd / self.peak_equity_usd) * 100.0
            if dd_pct > self.max_drawdown_pct:
                self.max_drawdown_pct = dd_pct

        day_idx = ts_ms // 86_400_000
        if self._last_day_idx < 0:
            self._last_day_idx = day_idx
            self._last_day_equity = equity_usd
        elif day_idx > self._last_day_idx:
            if self._last_day_equity > 0.0:
                ret = (equity_usd - self._last_day_equity) / self._last_day_equity
                self.daily_returns.append(ret)
            self._last_day_idx = day_idx
            self._last_day_equity = equity_usd

        if record_curve_point:
            self.equity_curve.append((ts_ms, round(equity_usd, 4)))
