# Path: ClavaBacktest/run_oos_study.py
# Role: CLI-запуск 5-летнего теста: оптимизация N на In-Sample (3 года) + проверка Out-of-Sample (2 года)

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("CLAVA_BACKTEST", "1")
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ClavaBot.ANALYTICS.report_formatter import ReportFormatter
from ClavaBacktest.ENGINE.tournament_runner import TournamentRunner


def main() -> None:
    skip_dl = "--skip-download" in sys.argv
    passport_name = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "choch_4h_base.json"
    bt_dir = Path(__file__).resolve().parent
    runner = TournamentRunner(bt_dir)
    payload = runner.run_oos_study(base_passport_name=passport_name, skip_download=skip_dl)

    print(f"\n=== In-Sample (3y) Optimization for {passport_name} ===")
    print(ReportFormatter.format_ascii_table(payload["in_sample_results"]))
    print(f"\n=== Out-of-Sample (2y Sealed) | Best N = {payload['best_swing_n']} ===")
    print(ReportFormatter.format_ascii_table([payload["oos_result"]]))


if __name__ == "__main__":
    main()
