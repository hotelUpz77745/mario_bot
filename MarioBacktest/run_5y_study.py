# Path: MarioBacktest/run_5y_study.py
# Role: 5-летний 1m-тест (3 года In-Sample + 2 года Out-of-Sample) и сравнение 1% Risk vs Hron (1435$/20x/1200$)

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


def _clone_n(base: StrategyPassport, n: int, tag: str) -> StrategyPassport:
    new_params = dict(base.signal.params)
    new_params["left_candles"] = n
    new_params["right_candles"] = n
    new_sig = dataclasses.replace(base.signal, params=new_params)
    return dataclasses.replace(base, strategy_id=f"{base.strategy_id}_{tag}_N{n}", signal=new_sig)


def main() -> None:
    skip_dl = "--skip-download" in sys.argv
    bt_dir = Path(__file__).resolve().parent
    tr = TournamentRunner(bt_dir)
    oos_cfg = tr.bt_cfg["oos_study"]
    strat_dir = tr.bot_dir / str(tr.app_cfg["paths"]["strategies_dir"])

    passport_names = [
        "choch_4h_weekdays_ny.json",
        "choch_4h_base.json",
        "choch_4h_weekdays_ny_hron.json",
        "choch_4h_base_hron.json",
    ]
    passports = [StrictConfigLoader.load_strategy_passport(strat_dir / n) for n in passport_names]
    all_syms = sorted({s for p in passports for s in p.symbols})
    warmup_days = int(oos_cfg["warmup_days"])
    n_candidates = [int(n) for n in oos_cfg["swing_n_candidates"]]
    opt_metric = str(oos_cfg["optimization_metric"])

    # 1. Загрузка и прогрев In-Sample (3 года: 2021-11-01 .. 2024-10-31)
    is_dataset = tr.load_dataset_for_dates(
        symbols=all_syms,
        start_date=str(oos_cfg["in_sample_start_date"]),
        end_date=str(oos_cfg["in_sample_end_date"]),
        warmup_days=warmup_days,
        skip_download=skip_dl,
    )

    is_base_summaries: List[Dict[str, Any]] = []
    n_grid_rows: List[Dict[str, Any]] = []
    best_n_map: Dict[str, int] = {}

    for p in passports:
        base_res = tr.runner.run_passport(
            dataclasses.replace(p, strategy_id=f"{p.strategy_id}_IS3Y"),
            is_dataset,
        )
        is_base_summaries.append(base_res)

        if p.capital.risk_per_trade_ratio is not None:
            best_n = int(p.signal.params["left_candles"])
            best_score = float("-inf")
            for n in n_candidates:
                v_res = tr.runner.run_passport(_clone_n(p, n, "IS3Y"), is_dataset)
                n_grid_rows.append(v_res)
                if float(v_res[opt_metric]) > best_score:
                    best_score = float(v_res[opt_metric])
                    best_n = n
            best_n_map[p.strategy_id] = best_n
        else:
            best_n_map[p.strategy_id] = int(p.signal.params["left_candles"])

    del is_dataset
    gc.collect()

    # 2. Out-of-Sample (2 запечатанных года: 2024-11-01 .. 2026-11-18)
    oos_dataset = tr.load_dataset_for_dates(
        symbols=all_syms,
        start_date=str(oos_cfg["oos_start_date"]),
        end_date=str(oos_cfg["oos_end_date"]),
        warmup_days=warmup_days,
        skip_download=skip_dl,
    )

    oos_base_summaries: List[Dict[str, Any]] = []
    oos_best_summaries: List[Dict[str, Any]] = []
    for p in passports:
        def_n = int(p.signal.params["left_candles"])
        res_default = tr.runner.run_passport(
            dataclasses.replace(p, strategy_id=f"{p.strategy_id}_OOS2Y_N{def_n}"),
            oos_dataset,
        )
        oos_base_summaries.append(res_default)

        bn = best_n_map[p.strategy_id]
        if bn != def_n:
            res_opt = tr.runner.run_passport(_clone_n(p, bn, "OOS2Y_OPT"), oos_dataset)
            oos_best_summaries.append(res_opt)
        else:
            oos_best_summaries.append(res_default)

    del oos_dataset
    gc.collect()

    print(f"\n=== 1. In-Sample 3 Years ({oos_cfg['in_sample_start_date']} .. {oos_cfg['in_sample_end_date']}) ===")
    print(ReportFormatter.format_ascii_table(is_base_summaries))

    print(f"\n=== 2. In-Sample Swing N Grid ({opt_metric} optimization) ===")
    print(ReportFormatter.format_ascii_table(n_grid_rows))
    print("Best N per strategy:", json.dumps(best_n_map))

    print(f"\n=== 3. Out-of-Sample 2 Sealed Years ({oos_cfg['oos_start_date']} .. {oos_cfg['oos_end_date']}) | Default N=3 ===")
    print(ReportFormatter.format_ascii_table(oos_base_summaries))

    print(f"\n=== 4. Out-of-Sample 2 Sealed Years | Best IS N ===")
    print(ReportFormatter.format_ascii_table(oos_best_summaries))

    payload = {
        "oos_cfg": oos_cfg,
        "best_n_map": best_n_map,
        "is_base": is_base_summaries,
        "is_n_grid": n_grid_rows,
        "oos_default": oos_base_summaries,
        "oos_best_n": oos_best_summaries,
    }
    ReportFormatter.export_json(tr.reports_dir / "study_5y_oos.json", payload)


if __name__ == "__main__":
    main()
