# Path: MarioBacktest/ENGINE/walk_forward_validator.py
# Role: Строгий Walk-Forward валидатор (3+2 запечатанный OOS и скользящие фолды против переобучения)

from __future__ import annotations

import dataclasses
import gc
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from MarioBot.CORE.config_loader import StrategyPassport
from MarioBacktest.ENGINE.backtest_runner import BacktestRunner
from MarioBacktest.ENGINE.vision_parser import Aligned1mDataset, BinanceVision1mParser


def _add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return date(y, m, day)


def _date_to_utc_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


class WalkForwardValidator:
    """
    Отдельный валидатор строгого Walk-Forward тестирования:
    1. Anchored Split (например 3 года IS -> 2 года запечатанный OOS).
    2. Rolling Folds (скользящие окна IS -> OOS по всему 5-летнему периоду для оценки устойчивости).
    """

    def __init__(self, parser: BinanceVision1mParser, runner: BacktestRunner):
        self.parser = parser
        self.runner = runner

    def _load_slice(
        self,
        symbols: List[str],
        start_d: date,
        end_d: date,
        warmup_days: int,
        skip_download: bool,
    ) -> Aligned1mDataset:
        sim_start_ms = _date_to_utc_ms(start_d)
        sim_end_excl_ms = _date_to_utc_ms(end_d + timedelta(days=1))
        warmup_start_ms = sim_start_ms - (warmup_days * 86_400_000)
        return self.parser.build_aligned_dataset(
            symbols=symbols,
            warmup_start_ms=warmup_start_ms,
            sim_start_ms=sim_start_ms,
            sim_end_exclusive_ms=sim_end_excl_ms,
            skip_download=skip_download,
        )

    @staticmethod
    def _clone_swing_n(passport: StrategyPassport, n: int, tag: str) -> StrategyPassport:
        new_params = dict(passport.signal.params)
        new_params["left_candles"] = n
        new_params["right_candles"] = n
        new_sig = dataclasses.replace(passport.signal, params=new_params)
        return dataclasses.replace(passport, strategy_id=f"{passport.strategy_id}_{tag}_N{n}", signal=new_sig)

    def run_rolling_walk_forward(
        self,
        passports: List[StrategyPassport],
        start_date_str: str,
        end_date_str: str,
        train_months: int,
        test_months: int,
        warmup_days: int,
        n_candidates: List[int],
        opt_metric: str,
        skip_download: bool = False,
    ) -> Dict[str, Any]:
        start_d = date.fromisoformat(start_date_str)
        end_d = date.fromisoformat(end_date_str)

        folds: List[Tuple[date, date, date, date]] = []
        cursor = start_d
        while True:
            is_start = cursor
            is_end = _add_months(is_start, train_months) - timedelta(days=1)
            oos_start = is_end + timedelta(days=1)
            oos_end = min(end_d, _add_months(oos_start, test_months) - timedelta(days=1))
            if oos_start > end_d or (oos_end - oos_start).days < 14:
                break
            folds.append((is_start, is_end, oos_start, oos_end))
            cursor = _add_months(cursor, test_months)

        per_strategy_folds: Dict[str, List[Dict[str, Any]]] = {p.strategy_id: [] for p in passports}
        all_syms = sorted({s for p in passports for s in p.symbols})

        for idx, (is_s, is_e, oos_s, oos_e) in enumerate(folds, start=1):
            is_ds = self._load_slice(all_syms, is_s, is_e, warmup_days, skip_download)
            chosen_n: Dict[str, int] = {}
            for p in passports:
                best_n = int(p.signal.params["left_candles"])
                best_score = float("-inf")
                for n in n_candidates:
                    res = self.runner.run_passport(self._clone_swing_n(p, n, f"F{idx}_IS"), is_ds)
                    if float(res[opt_metric]) > best_score:
                        best_score = float(res[opt_metric])
                        best_n = n
                chosen_n[p.strategy_id] = best_n
            del is_ds
            gc.collect()

            oos_ds = self._load_slice(all_syms, oos_s, oos_e, warmup_days, skip_download)
            for p in passports:
                bn = chosen_n[p.strategy_id]
                oos_res = self.runner.run_passport(self._clone_swing_n(p, bn, f"F{idx}_OOS"), oos_ds)
                oos_res["fold"] = idx
                oos_res["oos_period"] = f"{oos_s.isoformat()}..{oos_e.isoformat()}"
                oos_res["selected_n"] = bn
                per_strategy_folds[p.strategy_id].append(oos_res)
            del oos_ds
            gc.collect()

        aggregate: List[Dict[str, Any]] = []
        for p in passports:
            f_list = per_strategy_folds[p.strategy_id]
            pos_folds = sum(1 for f in f_list if float(f["net_pnl_usd"]) > 0.0)
            total_pnl = sum(float(f["net_pnl_usd"]) for f in f_list)
            total_trades = sum(int(f["total_trades"]) for f in f_list)
            total_wins = sum(int(f["wins"]) for f in f_list)
            avg_dd_pct = sum(float(f["max_drawdown_pct"]) for f in f_list) / len(f_list) if f_list else 0.0
            aggregate.append({
                "strategy_id": p.strategy_id,
                "folds_count": len(f_list),
                "positive_folds": pos_folds,
                "fold_survival_pct": round((pos_folds / len(f_list)) * 100.0, 2) if f_list else 0.0,
                "total_oos_net_pnl_usd": round(total_pnl, 2),
                "total_oos_roi_on_start_pct": round((total_pnl / p.capital.start_balance_usd) * 100.0, 2),
                "oos_winrate_pct": round((total_wins / total_trades) * 100.0, 2) if total_trades > 0 else 0.0,
                "total_oos_trades": total_trades,
                "avg_fold_max_dd_pct": round(avg_dd_pct, 2),
                "selected_n_sequence": [int(f["selected_n"]) for f in f_list],
            })

        return {
            "folds": [f"{a.isoformat()}..{b.isoformat()} -> {c.isoformat()}..{d.isoformat()}" for a, b, c, d in folds],
            "aggregate": aggregate,
            "per_strategy_folds": per_strategy_folds,
        }

    def run_fixed_rolling_folds(
        self,
        passports: List[StrategyPassport],
        start_date_str: str,
        end_date_str: str,
        step_months: int,
        warmup_days: int,
        skip_download: bool = False,
    ) -> Dict[str, Any]:
        """Оценка фиксированных паспортов по независимым последовательным окнам step_months (без подбора параметров)."""
        start_d = date.fromisoformat(start_date_str)
        end_d = date.fromisoformat(end_date_str)

        windows: List[Tuple[date, date]] = []
        cursor = start_d
        while cursor <= end_d:
            w_end = min(end_d, _add_months(cursor, step_months) - timedelta(days=1))
            if (w_end - cursor).days >= 14:
                windows.append((cursor, w_end))
            cursor = _add_months(cursor, step_months)

        per_strategy: Dict[str, List[Dict[str, Any]]] = {p.strategy_id: [] for p in passports}
        all_syms = sorted({s for p in passports for s in p.symbols})

        for idx, (w_s, w_e) in enumerate(windows, start=1):
            ds = self._load_slice(all_syms, w_s, w_e, warmup_days, skip_download)
            for p in passports:
                res = self.runner.run_passport(
                    dataclasses.replace(p, strategy_id=f"{p.strategy_id}_W{idx}"),
                    ds,
                )
                res["window"] = idx
                res["period"] = f"{w_s.isoformat()}..{w_e.isoformat()}"
                per_strategy[p.strategy_id].append(res)
            del ds
            gc.collect()

        aggregate: List[Dict[str, Any]] = []
        for p in passports:
            w_list = per_strategy[p.strategy_id]
            pos = sum(1 for w in w_list if float(w["net_pnl_usd"]) > 0.0)
            tot_pnl = sum(float(w["net_pnl_usd"]) for w in w_list)
            tot_tr = sum(int(w["total_trades"]) for w in w_list)
            tot_wins = sum(int(w["wins"]) for w in w_list)
            avg_dd = sum(float(w["max_drawdown_pct"]) for w in w_list) / len(w_list) if w_list else 0.0
            aggregate.append({
                "strategy_id": p.strategy_id,
                "windows_count": len(w_list),
                "positive_windows": pos,
                "window_survival_pct": round((pos / len(w_list)) * 100.0, 2) if w_list else 0.0,
                "total_net_pnl_usd": round(tot_pnl, 2),
                "total_roi_on_start_pct": round((tot_pnl / p.capital.start_balance_usd) * 100.0, 2),
                "winrate_pct": round((tot_wins / tot_tr) * 100.0, 2) if tot_tr > 0 else 0.0,
                "total_trades": tot_tr,
                "avg_window_max_dd_pct": round(avg_dd, 2),
                "per_window_pnl_usd": [float(w["net_pnl_usd"]) for w in w_list],
            })

        return {
            "windows": [f"{a.isoformat()}..{b.isoformat()}" for a, b in windows],
            "aggregate": aggregate,
            "per_strategy": per_strategy,
        }
