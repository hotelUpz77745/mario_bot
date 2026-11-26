# Path: MarioBacktest/run_markov_5y.py
# Role: 5-летний 1m-прогон лучшей стратегии с MarkovRegimeFilter + Strict Walk-Forward

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
from MarioBot.CORE.config_loader import StrategyPassport, StrictConfigLoader
from MarioBacktest.ENGINE.tournament_runner import TournamentRunner
from MarioBacktest.ENGINE.walk_forward_validator import WalkForwardValidator


def main() -> None:
    skip_dl = "--skip-download" in sys.argv
    bt_dir = Path(__file__).resolve().parent
    tr = TournamentRunner(bt_dir)
    oos_cfg = tr.bt_cfg["oos_study"]
    strat_dir = tr.bot_dir / str(tr.app_cfg["paths"]["strategies_dir"])

    passport_names = [
        "choch_4h_weekdays_ny.json",
        "choch_4h_weekdays_ny_markov_gate.json",
        "choch_4h_weekdays_ny_markov_strict.json",
        "choch_4h_weekdays_ny_hron.json",
        "choch_4h_weekdays_ny_markov_hron.json",
    ]
    passports = [StrictConfigLoader.load_strategy_passport(strat_dir / n) for n in passport_names]
    all_syms = sorted({s for p in passports for s in p.symbols})
    warmup_days = int(oos_cfg["warmup_days"])

    # 1. Полный 5-летний отрезок (In-Sample 3y + Out-of-Sample 2y по отдельности, с фиксированным N=3)
    is_ds = tr.load_dataset_for_dates(
        symbols=all_syms,
        start_date=str(oos_cfg["in_sample_start_date"]),
        end_date=str(oos_cfg["in_sample_end_date"]),
        warmup_days=warmup_days,
        skip_download=skip_dl,
    )
    is_results = [
        tr.runner.run_passport(dataclasses.replace(p, strategy_id=f"{p.strategy_id}_IS3Y"), is_ds)
        for p in passports
    ]
    del is_ds
    gc.collect()

    oos_ds = tr.load_dataset_for_dates(
        symbols=all_syms,
        start_date=str(oos_cfg["oos_start_date"]),
        end_date=str(oos_cfg["oos_end_date"]),
        warmup_days=warmup_days,
        skip_download=skip_dl,
    )
    oos_results = [
        tr.runner.run_passport(dataclasses.replace(p, strategy_id=f"{p.strategy_id}_OOS2Y"), oos_ds)
        for p in passports
    ]
    del oos_ds
    gc.collect()

    # 2. Rolling Walk-Forward (18 мес train -> 6 мес test по всей 5-летке) для 1% Risk-паспортов
    wf_passports = [p for p in passports if p.capital.risk_per_trade_ratio is not None]
    wf_validator = WalkForwardValidator(tr.parser, tr.runner)
    wf_report = wf_validator.run_rolling_folds if hasattr(wf_validator, "run_rolling_folds") else wf_validator.run_rolling_walk_forward
    wf_data = wf_report(
        passports=wf_passports,
        start_date_str=str(oos_cfg["in_sample_start_date"]),
        end_date_str=str(oos_cfg["oos_end_date"]),
        train_months=18,
        test_months=6,
        warmup_days=warmup_days,
        n_candidates=[2, 3, 4, 5, 6],
        opt_metric=str(oos_cfg["optimization_metric"]),
        skip_download=True,
    )

    print(f"\n=== 1. In-Sample 3 Years ({oos_cfg['in_sample_start_date']} .. {oos_cfg['in_sample_end_date']}) ===")
    print(ReportFormatter.format_ascii_table(is_results))

    print(f"\n=== 2. Out-of-Sample 2 Sealed Years ({oos_cfg['oos_start_date']} .. {oos_cfg['oos_end_date']}) ===")
    print(ReportFormatter.format_ascii_table(oos_results))
    for r in oos_results:
        print(f"  {r['strategy_id']} L/S:", json.dumps(r["long_short"]))

    print("\n=== 3. Strict Rolling Walk-Forward (18m Train -> 6m OOS Folds across 5 Years) ===")
    print(json.dumps(wf_data["aggregate"], ensure_ascii=False, indent=2))

    ReportFormatter.export_json(
        tr.reports_dir / "markov_5y_walkforward.json",
        {
            "is_3y": is_results,
            "oos_2y": oos_results,
            "rolling_walk_forward": wf_data,
        },
    )


if __name__ == "__main__":
    main()
