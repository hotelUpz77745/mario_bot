# Path: MarioBacktest/run_notes_strategies_5y.py
# Role: 5-летний 1m-тест новых стратегий из notes.md и расчёт единой градации всех стратегий

from __future__ import annotations

import dataclasses
import gc
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

os.environ.setdefault("MARIO_BACKTEST", "1")
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from MarioBot.ANALYTICS.report_formatter import ReportFormatter
from MarioBot.CORE.config_loader import StrictConfigLoader
from MarioBacktest.ENGINE.tournament_runner import TournamentRunner
from MarioBacktest.ENGINE.walk_forward_validator import WalkForwardValidator


def _compute_composite_score(
    is_row: Dict[str, Any],
    oos_row: Dict[str, Any],
    win_row: Dict[str, Any],
) -> float:
    """
    Прозрачный композитный скор устойчивости (0..100+ баллов):
    - 40% вес: OOS 2Y Profit Factor (нормированный) и Calmar
    - 30% вес: Выживаемость по 11 полугодовым окнам (%)
    - 15% вес: In-Sample 3Y ROI (%)
    - 15% вес: Штраф за максимальную просадку на OOS 2Y
    """
    oos_pf = max(0.0, min(2.0, float(oos_row["profit_factor"])))
    oos_calmar = max(-1.0, min(2.0, float(oos_row["calmar_ratio"])))
    pf_score = (oos_pf / 2.0) * 25.0
    calmar_score = ((oos_calmar + 1.0) / 3.0) * 15.0

    survival_score = (float(win_row["window_survival_pct"]) / 100.0) * 30.0

    is_roi_clamped = max(-50.0, min(100.0, float(is_row["roi_pct"])))
    is_score = ((is_roi_clamped + 50.0) / 150.0) * 15.0

    oos_dd = float(oos_row["max_drawdown_pct"])
    dd_safety = max(0.0, 100.0 - oos_dd * 1.6) / 100.0 * 15.0

    return round(pf_score + calmar_score + survival_score + is_score + dd_safety, 2)


def main() -> None:
    skip_dl = "--skip-download" in sys.argv
    bt_dir = Path(__file__).resolve().parent
    tr = TournamentRunner(bt_dir)
    oos_cfg = tr.bt_cfg["oos_study"]
    hy_cfg = tr.bt_cfg["half_year"]
    strat_dir = tr.bot_dir / str(tr.app_cfg["paths"]["strategies_dir"])

    new_names = [
        "bb_breakout_4h.json",
        "bb_breakout_4h_markov.json",
        "ema_pullback_4h.json",
        "ema_pullback_4h_markov.json",
        "williams_bailout_4h.json",
        "williams_bailout_4h_markov.json",
    ]
    passports = [StrictConfigLoader.load_strategy_passport(strat_dir / n) for n in new_names]
    all_syms = sorted({s for p in passports for s in p.symbols})
    warmup_days = 60

    # 1. Half-Year
    hy_ds = tr.load_dataset_for_dates(
        symbols=all_syms,
        start_date=str(hy_cfg["start_date"]),
        end_date=str(hy_cfg["end_date"]),
        warmup_days=warmup_days,
        skip_download=skip_dl,
    )
    hy_results = [tr.runner.run_passport(p, hy_ds) for p in passports]
    del hy_ds
    gc.collect()

    # 2. IS 3Y
    is_ds = tr.load_dataset_for_dates(
        symbols=all_syms,
        start_date=str(oos_cfg["in_sample_start_date"]),
        end_date=str(oos_cfg["in_sample_end_date"]),
        warmup_days=warmup_days,
        skip_download=True,
    )
    is_results = [
        tr.runner.run_passport(dataclasses.replace(p, strategy_id=f"{p.strategy_id}_IS3Y"), is_ds)
        for p in passports
    ]
    del is_ds
    gc.collect()

    # 3. OOS 2Y
    oos_ds = tr.load_dataset_for_dates(
        symbols=all_syms,
        start_date=str(oos_cfg["oos_start_date"]),
        end_date=str(oos_cfg["oos_end_date"]),
        warmup_days=warmup_days,
        skip_download=True,
    )
    oos_results = [
        tr.runner.run_passport(dataclasses.replace(p, strategy_id=f"{p.strategy_id}_OOS2Y"), oos_ds)
        for p in passports
    ]
    del oos_ds
    gc.collect()

    # 4. 11 Sequential Half-Year Windows across 5 Years
    wf = WalkForwardValidator(tr.parser, tr.runner)
    rolling_6m = wf.run_fixed_rolling_folds(
        passports=passports,
        start_date_str=str(oos_cfg["in_sample_start_date"]),
        end_date_str=str(oos_cfg["oos_end_date"]),
        step_months=6,
        warmup_days=warmup_days,
        skip_download=True,
    )

    print(f"\n=== 1. Half-Year ({hy_cfg['start_date']} .. {hy_cfg['end_date']}) ===")
    print(ReportFormatter.format_ascii_table(hy_results))

    print(f"\n=== 2. In-Sample 3 Years ({oos_cfg['in_sample_start_date']} .. {oos_cfg['in_sample_end_date']}) ===")
    print(ReportFormatter.format_ascii_table(is_results))

    print(f"\n=== 3. Out-of-Sample 2 Sealed Years ({oos_cfg['oos_start_date']} .. {oos_cfg['oos_end_date']}) ===")
    print(ReportFormatter.format_ascii_table(oos_results))
    for r in oos_results:
        print(f"  {r['strategy_id']} exits={json.dumps(r['exit_reasons'])} L/S={json.dumps(r['long_short'])}")

    print("\n=== 4. 11 Sequential Half-Year Windows (New Strategies) ===")
    print(json.dumps(rolling_6m["aggregate"], ensure_ascii=False, indent=2))

    # 5. Единая градация всех $1000-стратегий из предыдущих отчётов + новых
    is_map: Dict[str, Dict[str, Any]] = {}
    oos_map: Dict[str, Dict[str, Any]] = {}
    win_map: Dict[str, Dict[str, Any]] = {}

    for report_file in ("ema_vs_choch_5y.json", "ema_9_21_vs_7_25_5y.json"):
        rp = tr.reports_dir / report_file
        if rp.exists():
            prev = json.loads(rp.read_text(encoding="utf-8"))
            for r in prev["is_3y"]:
                base_id = str(r["strategy_id"]).replace("_IS3Y", "")
                is_map[base_id] = r
            for r in prev["oos_2y"]:
                base_id = str(r["strategy_id"]).replace("_OOS2Y", "")
                oos_map[base_id] = r
            roll_key = "rolling_6m_5y"
            for r in prev[roll_key]["aggregate"]:
                win_map[str(r["strategy_id"])] = r

    for r in is_results:
        is_map[str(r["strategy_id"]).replace("_IS3Y", "")] = r
    for r in oos_results:
        oos_map[str(r["strategy_id"]).replace("_OOS2Y", "")] = r
    for r in rolling_6m["aggregate"]:
        win_map[str(r["strategy_id"])] = r

    leaderboard: List[Dict[str, Any]] = []
    for sid, win_r in win_map.items():
        if sid not in is_map or sid not in oos_map:
            continue
        is_r = is_map[sid]
        oos_r = oos_map[sid]
        if float(oos_r["start_balance_usd"]) != 1000.0:
            continue
        score = _compute_composite_score(is_r, oos_r, win_r)
        leaderboard.append({
            "strategy_id": sid,
            "score": score,
            "is_roi_pct": is_r["roi_pct"],
            "oos_roi_pct": oos_r["roi_pct"],
            "oos_wr_pct": oos_r["winrate_pct"],
            "oos_pf": oos_r["profit_factor"],
            "oos_calmar": oos_r["calmar_ratio"],
            "oos_max_dd_pct": oos_r["max_drawdown_pct"],
            "windows_survival_pct": win_r["window_survival_pct"],
            "windows_ratio": f"{win_r['positive_windows']}/{win_r['windows_count']}",
            "windows_net_pnl": win_r["total_net_pnl_usd"],
        })

    leaderboard.sort(key=lambda x: float(x["score"]), reverse=True)
    print("\n=== 5. UNIFIED LEADERBOARD (All 5Y Strategies Ranked Best -> Worst) ===")
    for rank, row in enumerate(leaderboard, start=1):
        print(
            f"#{rank:02d} | {row['strategy_id']:36s} | Score={row['score']:5.2f} | "
            f"IS={row['is_roi_pct']:+6.2f}% | OOS={row['oos_roi_pct']:+6.2f}% (PF={row['oos_pf']:.2f}, "
            f"DD={row['oos_max_dd_pct']:5.2f}%) | 11W={row['windows_ratio']} ({row['windows_survival_pct']:5.1f}%)"
        )

    ReportFormatter.export_json(
        tr.reports_dir / "notes_strategies_5y.json",
        {
            "half_year": hy_results,
            "is_3y": is_results,
            "oos_2y": oos_results,
            "rolling_6m_5y": rolling_6m,
            "unified_leaderboard": leaderboard,
        },
    )


if __name__ == "__main__":
    main()
