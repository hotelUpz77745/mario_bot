# Path: MarioBacktest/run_halfyear.py
# Role: CLI-запуск 1-минутного отчёта за последние полгода (choch_4h_weekdays_ny и choch_4h_base)

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("MARIO_BACKTEST", "1")
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from MarioBot.ANALYTICS.report_formatter import ReportFormatter
from MarioBacktest.ENGINE.tournament_runner import TournamentRunner


def main() -> None:
    skip_dl = "--skip-download" in sys.argv
    bt_dir = Path(__file__).resolve().parent
    runner = TournamentRunner(bt_dir)
    summaries = runner.run_half_year_study(skip_download=skip_dl)
    hy = runner.bt_cfg["half_year"]

    print(f"\n=== MarioBacktest Half-Year Study ({hy['start_date']} .. {hy['end_date']}, 1m Execution) ===")
    print(ReportFormatter.format_ascii_table(summaries))
    print("\n=== Detailed Diagnostics ===")
    for s in summaries:
        print(f"\n--- {s['strategy_id']} ---")
        print("Long/Short:", json.dumps(s["long_short"]))
        print("Diagnostics:", json.dumps(s["trade_diagnostics"]))
        print("Monthly PnL:", json.dumps(s["monthly_pnl_usd"]))
        print("Per-Coin:", json.dumps(s["per_coin"]))


if __name__ == "__main__":
    main()
