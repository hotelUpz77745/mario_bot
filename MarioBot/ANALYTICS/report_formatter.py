# Path: MarioBot/ANALYTICS/report_formatter.py
# Role: Форматирование сравнительных таблиц турниров паспортов (Console/Markdown/JSON/CSV)

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List


class ReportFormatter:
    """Формирует сводные отчёты по одному или нескольким паспортам стратегий."""

    HEADERS = [
        "Strategy",
        "NetPnL$",
        "ROI%",
        "Winrate%",
        "Trades(W/L)",
        "MaxDD$(%)",
        "Calmar",
        "Sharpe",
        "PF",
        "Fees$",
    ]

    @classmethod
    def build_rows(cls, summaries: List[Dict[str, Any]]) -> List[List[str]]:
        ordered = sorted(summaries, key=lambda x: float(x["net_pnl_usd"]), reverse=True)
        rows: List[List[str]] = []
        for s in ordered:
            rows.append([
                str(s["strategy_id"]),
                f"{s['net_pnl_usd']:+.2f}",
                f"{s['roi_pct']:+.2f}%",
                f"{s['winrate_pct']:.2f}%",
                f"{s['total_trades']}({s['wins']}/{s['losses']})",
                f"{s['max_drawdown_usd']:.2f}({s['max_drawdown_pct']:.2f}%)",
                f"{s['calmar_ratio']:.2f}",
                f"{s['sharpe_ratio']:.2f}",
                f"{s['profit_factor']:.2f}",
                f"{s['total_fees_usd']:.2f}",
            ])
        return rows

    @classmethod
    def format_ascii_table(cls, summaries: List[Dict[str, Any]]) -> str:
        rows = cls.build_rows(summaries)
        widths = [len(h) for h in cls.HEADERS]
        for r in rows:
            for idx, cell in enumerate(r):
                if len(cell) > widths[idx]:
                    widths[idx] = len(cell)

        def _fmt(cells: List[str]) -> str:
            return " | ".join(
                cells[i].ljust(widths[i]) if i == 0 else cells[i].rjust(widths[i])
                for i in range(len(cells))
            )

        sep = "-+-".join("-" * w for w in widths)
        lines = [_fmt(cls.HEADERS), sep]
        lines.extend(_fmt(r) for r in rows)
        return "\n".join(lines)

    @classmethod
    def format_markdown_table(cls, summaries: List[Dict[str, Any]]) -> str:
        rows = cls.build_rows(summaries)
        esc_h = [h.replace("$", "\\$") for h in cls.HEADERS]
        sep = [":---" if i == 0 else "---:" for i in range(len(esc_h))]
        lines = [
            "| " + " | ".join(esc_h) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for r in rows:
            lines.append("| " + " | ".join(c.replace("$", "\\$") for c in r) + " |")
        return "\n".join(lines)

    @staticmethod
    def export_json(path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    @classmethod
    def export_csv(cls, path: Path, summaries: List[Dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = cls.build_rows(summaries)
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(cls.HEADERS)
            writer.writerows(rows)
