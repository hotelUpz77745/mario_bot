# Path: MarioBot/ANALYTICS/metrics_engine.py
# Role: Расчёт ключевых метрик эффективности (Winrate, ROI, Calmar, Sharpe, Net PnL, Max DD)

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Dict, List
from MarioBot.ANALYTICS.equity_tracker import EquityTracker
from MarioBot.CORE.models import ClosedTrade


class AnalyticsMetricsEngine:
    """
    Рассчитывает полный набор метрик портфеля и пер-монетной статистики:
    Winrate (%), ROI (%), Net PnL ($), Max Drawdown ($ / %), Calmar Ratio, Sharpe Ratio, Profit Factor.
    """

    @staticmethod
    def compute(
        strategy_id: str,
        start_balance_usd: float,
        end_equity_usd: float,
        trades: List[ClosedTrade],
        tracker: EquityTracker,
        sim_start_ms: int,
        sim_end_ms: int,
        annualization_days: int,
    ) -> Dict[str, Any]:
        total_trades = len(trades)
        wins = [t for t in trades if t.net_pnl_usd > 0.0]
        losses = [t for t in trades if t.net_pnl_usd <= 0.0]

        win_count = len(wins)
        loss_count = len(losses)
        winrate_pct = (win_count / total_trades) * 100.0 if total_trades > 0 else 0.0

        net_pnl_usd = end_equity_usd - start_balance_usd
        roi_pct = (net_pnl_usd / start_balance_usd) * 100.0 if start_balance_usd > 0.0 else 0.0
        total_fees_usd = sum(t.fees_usd for t in trades)

        gross_wins = sum(t.net_pnl_usd for t in wins)
        gross_losses_abs = abs(sum(t.net_pnl_usd for t in losses))
        profit_factor = (gross_wins / gross_losses_abs) if gross_losses_abs > 0.0 else (
            999.0 if gross_wins > 0.0 else 0.0
        )
        expectancy_usd = (net_pnl_usd / total_trades) if total_trades > 0 else 0.0

        duration_days = max(1.0, (sim_end_ms - sim_start_ms) / 86_400_000.0)
        annualized_roi_pct = roi_pct * (float(annualization_days) / duration_days)
        calmar_ratio = (
            annualized_roi_pct / tracker.max_drawdown_pct
            if tracker.max_drawdown_pct > 0.0
            else 0.0
        )

        sharpe_ratio = AnalyticsMetricsEngine._compute_sharpe(
            tracker.daily_returns,
            annualization_days=annualization_days,
        )

        per_coin = AnalyticsMetricsEngine._compute_per_coin(trades)
        long_short = AnalyticsMetricsEngine._compute_long_short(trades)
        monthly_pnl_usd = AnalyticsMetricsEngine._compute_monthly_pnl(trades)
        trade_diagnostics = AnalyticsMetricsEngine._compute_trade_diagnostics(trades)

        return {
            "strategy_id": strategy_id,
            "start_balance_usd": round(start_balance_usd, 2),
            "end_equity_usd": round(end_equity_usd, 2),
            "net_pnl_usd": round(net_pnl_usd, 2),
            "roi_pct": round(roi_pct, 2),
            "winrate_pct": round(winrate_pct, 2),
            "total_trades": total_trades,
            "wins": win_count,
            "losses": loss_count,
            "max_drawdown_usd": round(tracker.max_drawdown_usd, 2),
            "max_drawdown_pct": round(tracker.max_drawdown_pct, 2),
            "peak_equity_usd": round(tracker.peak_equity_usd, 2),
            "min_equity_usd": round(tracker.min_equity_usd, 2),
            "calmar_ratio": round(calmar_ratio, 3),
            "sharpe_ratio": round(sharpe_ratio, 3),
            "profit_factor": round(profit_factor, 3),
            "expectancy_usd": round(expectancy_usd, 2),
            "total_fees_usd": round(total_fees_usd, 2),
            "long_short": long_short,
            "monthly_pnl_usd": monthly_pnl_usd,
            "trade_diagnostics": trade_diagnostics,
            "per_coin": per_coin,
        }

    @staticmethod
    def _compute_sharpe(daily_returns: List[float], annualization_days: int) -> float:
        n = len(daily_returns)
        if n < 2:
            return 0.0
        mean_r = sum(daily_returns) / float(n)
        var_r = sum((r - mean_r) ** 2 for r in daily_returns) / float(n - 1)
        std_r = math.sqrt(var_r)
        if std_r <= 1e-12:
            return 0.0
        return (mean_r / std_r) * math.sqrt(float(annualization_days))

    @staticmethod
    def _compute_per_coin(trades: List[ClosedTrade]) -> Dict[str, Dict[str, Any]]:
        grouped: Dict[str, List[ClosedTrade]] = {}
        for tr in trades:
            grouped.setdefault(tr.symbol, []).append(tr)

        result: Dict[str, Dict[str, Any]] = {}
        for sym, sym_trades in sorted(grouped.items()):
            cnt = len(sym_trades)
            wins = sum(1 for t in sym_trades if t.net_pnl_usd > 0.0)
            net_pnl = sum(t.net_pnl_usd for t in sym_trades)
            fees = sum(t.fees_usd for t in sym_trades)
            result[sym] = {
                "trades": cnt,
                "wins": wins,
                "losses": cnt - wins,
                "winrate_pct": round((wins / cnt) * 100.0, 2) if cnt > 0 else 0.0,
                "net_pnl_usd": round(net_pnl, 2),
                "fees_usd": round(fees, 2),
            }
        return result

    @staticmethod
    def _compute_long_short(trades: List[ClosedTrade]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for side in ("LONG", "SHORT"):
            sub = [t for t in trades if t.side == side]
            cnt = len(sub)
            wins = sum(1 for t in sub if t.net_pnl_usd > 0.0)
            out[side] = {
                "trades": cnt,
                "wins": wins,
                "losses": cnt - wins,
                "winrate_pct": round((wins / cnt) * 100.0, 2) if cnt > 0 else 0.0,
                "net_pnl_usd": round(sum(t.net_pnl_usd for t in sub), 2),
            }
        return out

    @staticmethod
    def _compute_monthly_pnl(trades: List[ClosedTrade]) -> Dict[str, float]:
        monthly: Dict[str, float] = {}
        for t in trades:
            ym = datetime.fromtimestamp(t.exit_ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m")
            monthly[ym] = monthly.get(ym, 0.0) + t.net_pnl_usd
        return {ym: round(val, 2) for ym, val in sorted(monthly.items())}

    @staticmethod
    def _compute_trade_diagnostics(trades: List[ClosedTrade]) -> Dict[str, Any]:
        if not trades:
            return {
                "max_win_streak": 0,
                "max_loss_streak": 0,
                "avg_hold_hours": 0.0,
                "avg_sl_dist_pct": 0.0,
                "avg_win_usd": 0.0,
                "avg_loss_usd": 0.0,
            }
        max_w = max_l = cur_w = cur_l = 0
        hold_hours: List[float] = []
        sl_dists_pct: List[float] = []
        win_pnls: List[float] = []
        loss_pnls: List[float] = []
        for t in trades:
            if t.net_pnl_usd > 0.0:
                cur_w += 1
                cur_l = 0
                max_w = max(max_w, cur_w)
                win_pnls.append(t.net_pnl_usd)
            else:
                cur_l += 1
                cur_w = 0
                max_l = max(max_l, cur_l)
                loss_pnls.append(t.net_pnl_usd)
            hold_hours.append(max(0.0, (t.exit_ts_ms - t.entry_ts_ms) / 3_600_000.0))
            if t.entry_price > 0.0 and t.stop_loss is not None:
                sl_dists_pct.append(abs(t.entry_price - t.stop_loss) / t.entry_price * 100.0)

        return {
            "max_win_streak": max_w,
            "max_loss_streak": max_l,
            "avg_hold_hours": round(sum(hold_hours) / len(hold_hours), 2),
            "avg_sl_dist_pct": round(sum(sl_dists_pct) / len(sl_dists_pct), 2) if sl_dists_pct else 0.0,
            "avg_win_usd": round(sum(win_pnls) / len(win_pnls), 2) if win_pnls else 0.0,
            "avg_loss_usd": round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else 0.0,
        }
