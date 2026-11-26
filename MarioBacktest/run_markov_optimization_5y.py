# Path: MarioBacktest/run_markov_optimization_5y.py
# Role: 5-летний 1m-бэктест baseline markov_pure_4h + всех форков SL и апгрейдов из notes2.md

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
from MarioBacktest.run_notes_strategies_5y import _compute_composite_score


def main() -> None:
    skip_dl = "--skip-download" in sys.argv
    bt_dir = Path(__file__).resolve().parent
    tr = TournamentRunner(bt_dir)
    oos_cfg = tr.bt_cfg["oos_study"]
    hy_cfg = tr.bt_cfg["half_year"]
    strat_dir = tr.bot_dir / str(tr.app_cfg["paths"]["strategies_dir"])

    passports = [StrictConfigLoader.load_strategy_passport(p) for p in sorted(strat_dir.glob("markov_pure_*.json"))]
    all_syms = sorted({s for p in passports for s in p.symbols})
    warmup_days = 60

    # 1. Half-Year
    hy_ds = tr.load_dataset_for_dates(all_syms, str(hy_cfg["start_date"]), str(hy_cfg["end_date"]), warmup_days, skip_dl)
    hy_results = [tr.runner.run_passport(p, hy_ds) for p in passports]
    del hy_ds
    gc.collect()

    # 2. IS 3Y
    is_ds = tr.load_dataset_for_dates(all_syms, str(oos_cfg["in_sample_start_date"]), str(oos_cfg["in_sample_end_date"]), warmup_days, True)
    is_results = [tr.runner.run_passport(dataclasses.replace(p, strategy_id=f"{p.strategy_id}_IS3Y"), is_ds) for p in passports]
    del is_ds
    gc.collect()

    # 3. OOS 2Y
    oos_ds = tr.load_dataset_for_dates(all_syms, str(oos_cfg["oos_start_date"]), str(oos_cfg["oos_end_date"]), warmup_days, True)
    oos_results = [tr.runner.run_passport(dataclasses.replace(p, strategy_id=f"{p.strategy_id}_OOS2Y"), oos_ds) for p in passports]
    del oos_ds
    gc.collect()

    # 4. 11 Sequential Half-Year Windows (для $1000-стратегий)
    base_passports = [p for p in passports if p.capital.start_balance_usd == 1000.0]
    wf = WalkForwardValidator(tr.parser, tr.runner)
    rolling_6m = wf.run_fixed_rolling_folds(
        passports=base_passports,
        start_date_str=str(oos_cfg["in_sample_start_date"]),
        end_date_str=str(oos_cfg["oos_end_date"]),
        step_months=6,
        warmup_days=warmup_days,
        skip_download=True,
    )

    print(f"\n=== 1. Half-Year ({hy_cfg['start_date']} .. {hy_cfg['end_date']}) ===")
    print(ReportFormatter.format_ascii_table(hy_results))
    print(f"\n=== 2. IS 3Y ({oos_cfg['in_sample_start_date']} .. {oos_cfg['in_sample_end_date']}) ===")
    print(ReportFormatter.format_ascii_table(is_results))
    print(f"\n=== 3. OOS 2Y ({oos_cfg['oos_start_date']} .. {oos_cfg['oos_end_date']}) ===")
    print(ReportFormatter.format_ascii_table(oos_results))
    for r in oos_results:
        print(f"  {r['strategy_id']} exits={json.dumps(r['exit_reasons'])}")

    is_map = {str(r["strategy_id"]).replace("_IS3Y", ""): r for r in is_results}
    oos_map = {str(r["strategy_id"]).replace("_OOS2Y", ""): r for r in oos_results}
    win_map = {str(r["strategy_id"]): r for r in rolling_6m["aggregate"]}

    ranked: List[Dict[str, Any]] = []
    for sid, win_r in win_map.items():
        is_r, oos_r = is_map[sid], oos_map[sid]
        ranked.append({
            "strategy_id": sid,
            "score": _compute_composite_score(is_r, oos_r, win_r),
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

    ranked.sort(key=lambda x: float(x["score"]), reverse=True)
    print("\n=== 4. MARKOV FAMILY RANKING (Baseline vs Smart Wide SL vs Notes2 Upgrades) ===")
    for rank, row in enumerate(ranked, start=1):
        print(
            f"#{rank:02d} | {row['strategy_id']:32s} | Score={row['score']:5.2f} | "
            f"IS={row['is_roi_pct']:+6.2f}% | OOS={row['oos_roi_pct']:+6.2f}% (WR={row['oos_wr_pct']:5.1f}%, PF={row['oos_pf']:.2f}, "
            f"Calmar={row['oos_calmar']:5.2f}, DD={row['oos_max_dd_pct']:5.2f}%) | 11W={row['windows_ratio']} ({row['windows_survival_pct']:5.1f}%)"
        )

    ReportFormatter.export_json(
        tr.reports_dir / "markov_optimization_5y.json",
        {
            "half_year": hy_results,
            "is_3y": is_results,
            "oos_2y": oos_results,
            "rolling_6m_5y": rolling_6m,
            "markov_ranking": ranked,
        },
    )


if __name__ == "__main__":
    main()
