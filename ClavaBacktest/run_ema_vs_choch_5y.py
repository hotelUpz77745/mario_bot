# Path: ClavaBacktest/run_ema_vs_choch_5y.py
# Role: 5-летний 1m-тест сравнения EMA Cross 7/25 (4h/1d + Markov) и 4H CHoCH (3y IS + 2y OOS + 10 полугодий)

from __future__ import annotations

import dataclasses
import gc
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

os.environ.setdefault("CLAVA_BACKTEST", "1")
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ClavaBot.ANALYTICS.report_formatter import ReportFormatter
from ClavaBot.CORE.config_loader import StrictConfigLoader
from ClavaBacktest.ENGINE.tournament_runner import TournamentRunner
from ClavaBacktest.ENGINE.walk_forward_validator import WalkForwardValidator


def main() -> None:
    skip_dl = "--skip-download" in sys.argv
    bt_dir = Path(__file__).resolve().parent
    tr = TournamentRunner(bt_dir)
    oos_cfg = tr.bt_cfg["oos_study"]
    strat_dir = tr.bot_dir / str(tr.app_cfg["paths"]["strategies_dir"])

    passport_names = [
        "ema_cross_7_25_4h_markov.json",
        "ema_cross_7_25_4h.json",
        "ema_cross_7_25_1d.json",
        "ema_cross_7_25_1d_markov.json",
        "ema_cross_7_25_4h_markov_hron.json",
        "ema_cross_7_25_1d_hron.json",
        "choch_4h_weekdays_ny_markov_gate.json",
        "choch_4h_weekdays_ny.json",
    ]
    passports = [StrictConfigLoader.load_strategy_passport(strat_dir / n) for n in passport_names]
    all_syms = sorted({s for p in passports for s in p.symbols})
    warmup_days = 60  # достаточно для 1d EMA(25) и 20-дневной Марковской матрицы

    # 1. In-Sample 3 года (2021-11-01 .. 2024-10-31)
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

    # 2. Out-of-Sample 2 запечатанных года (2024-11-01 .. 2026-11-18)
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

    # 3. Проверка устойчивости по 10 последовательным полугодиям за 5 лет (для базовых $1000-паспортов)
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

    print(f"\n=== 1. In-Sample 3 Years ({oos_cfg['in_sample_start_date']} .. {oos_cfg['in_sample_end_date']}) ===")
    print(ReportFormatter.format_ascii_table(is_results))

    print(f"\n=== 2. Out-of-Sample 2 Sealed Years ({oos_cfg['oos_start_date']} .. {oos_cfg['oos_end_date']}) ===")
    print(ReportFormatter.format_ascii_table(oos_results))
    for r in oos_results:
        print(f"  {r['strategy_id']} L/S:", json.dumps(r["long_short"]))

    print("\n=== 3. 10 Sequential Half-Year Windows across 5 Years ($1000 Start) ===")
    print("Windows:", rolling_6m["windows"])
    print(json.dumps(rolling_6m["aggregate"], ensure_ascii=False, indent=2))

    ReportFormatter.export_json(
        tr.reports_dir / "ema_vs_choch_5y.json",
        {
            "is_3y": is_results,
            "oos_2y": oos_results,
            "rolling_6m_5y": rolling_6m,
        },
    )


if __name__ == "__main__":
    main()
