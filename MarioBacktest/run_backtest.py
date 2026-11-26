# Path: MarioBacktest/run_backtest.py
# Role: CLI-запуск 1-минутного турнира всех паспортов стратегий из MarioBot/CFG/strategies/

from __future__ import annotations

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
    summaries = runner.run_tournament(skip_download=skip_dl)
    print("\n=== MarioBacktest (1m Execution | 4H CHoCH Decision) ===")
    print(ReportFormatter.format_ascii_table(summaries))


if __name__ == "__main__":
    main()
