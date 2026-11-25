# Path: ClavaBacktest/run_ema_cross_halfyear.py
# Role: Полугодовой 1m-бэктест стратегии EMA Cross 7/25 на таймфреймах 1h, 4h и 1d

from __future__ import annotations

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


def main() -> None:
    skip_dl = "--skip-download" in sys.argv
    bt_dir = Path(__file__).resolve().parent
    tr = TournamentRunner(bt_dir)
    hy_cfg = tr.bt_cfg["half_year"]
    strat_dir = tr.bot_dir / str(tr.app_cfg["paths"]["strategies_dir"])

    passport_names = [
        "ema_cross_7_25_1h.json",
        "ema_cross_7_25_4h.json",
        "ema_cross_7_25_1d.json",
        "ema_cross_7_25_1h_markov.json",
        "ema_cross_7_25_4h_markov.json",
        "ema_cross_7_25_1d_markov.json",
        "ema_cross_7_25_4h_weekdays_ny.json",
        "ema_cross_7_25_1h_hron.json",
        "ema_cross_7_25_4h_hron.json",
        "ema_cross_7_25_1d_hron.json",
    ]
    passports = [StrictConfigLoader.load_strategy_passport(strat_dir / n) for n in passport_names]
    all_syms = sorted({s for p in passports for s in p.symbols})

    # 60 дней прогрева чтобы 1d EMA(25) полностью стабилизировалась к 2026-05-19
    dataset = tr.load_dataset_for_dates(
        symbols=all_syms,
        start_date=str(hy_cfg["start_date"]),
        end_date=str(hy_cfg["end_date"]),
        warmup_days=60,
        skip_download=skip_dl,
    )

    summaries: List[Dict[str, Any]] = [tr.runner.run_passport(p, dataset) for p in passports]
    print(f"\n=== EMA Cross 7/25 Half-Year ({hy_cfg['start_date']} .. {hy_cfg['end_date']}, 1m Execution) ===")
    print(ReportFormatter.format_ascii_table(summaries))
    print("\n=== Long/Short & Exit Breakdown ===")
    for s in summaries:
        print(
            f"{s['strategy_id']}: exits={json.dumps(s['exit_reasons'])} | "
            f"L/S={json.dumps(s['long_short'])} | avg_hold_h={s['trade_diagnostics']['avg_hold_hours']}"
        )

    ReportFormatter.export_json(
        tr.reports_dir / "ema_cross_halfyear.json",
        {"period": hy_cfg, "warmup_days": 60, "strategies": summaries},
    )
    md = "# EMA Cross 7/25 Half-Year\n\n" + ReportFormatter.format_markdown_table(summaries) + "\n"
    (tr.reports_dir / "ema_cross_halfyear.md").write_text(md, encoding="utf-8")


if __name__ == "__main__":
    main()
