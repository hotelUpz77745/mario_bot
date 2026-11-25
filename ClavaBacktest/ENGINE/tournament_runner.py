# Path: ClavaBacktest/ENGINE/tournament_runner.py
# Role: Загрузка паспортов, кэша 1m и мульти-прогон турниров + OOS оптимизация

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from ClavaBot.ANALYTICS.report_formatter import ReportFormatter
from ClavaBot.CORE.config_loader import StrategyPassport, StrictConfigLoader
from ClavaBot.CORE.spec_manager import SpecManager
from ClavaBacktest.ENGINE.backtest_runner import BacktestRunner
from ClavaBacktest.ENGINE.vision_parser import Aligned1mDataset, BinanceVision1mParser


def date_str_to_utc_ms(date_str: str) -> int:
    dt = datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class TournamentRunner:
    """Координирует загрузку 1m датасета и прогон нескольких вариаций паспортов."""

    def __init__(self, backtest_dir: Path):
        self.backtest_dir = Path(backtest_dir)
        self.bt_cfg = StrictConfigLoader.read_json(self.backtest_dir / "CFG" / "backtest.json")

        paths = self.bt_cfg["paths"]
        self.bot_dir = (self.backtest_dir / str(paths["bot_dir"])).resolve()
        self.cache_dir = (self.backtest_dir / str(paths["cache_1m_dir"])).resolve()
        self.extra_cache_dirs = [(self.backtest_dir / str(p)).resolve() for p in paths["extra_cache_dirs"]]
        self.reports_dir = (self.backtest_dir / str(paths["reports_dir"])).resolve()

        self.app_cfg = StrictConfigLoader.read_json(self.bot_dir / "CFG" / "app.json")
        self.spec_manager = SpecManager.from_app_config(self.bot_dir, self.app_cfg)
        self.exec_cfg = self.bt_cfg["execution"]

        self.parser = BinanceVision1mParser(
            cache_dir=self.cache_dir,
            extra_cache_dirs=self.extra_cache_dirs,
            max_workers=int(self.exec_cfg["download_workers"]),
            http_timeout_sec=float(self.exec_cfg["http_timeout_sec"]),
            http_retries=int(self.exec_cfg["http_retries"]),
        )
        self.runner = BacktestRunner(self.spec_manager, self.exec_cfg)

    def load_all_passports(self) -> List[StrategyPassport]:
        strat_dir = self.bot_dir / str(self.app_cfg["paths"]["strategies_dir"])
        passports: List[StrategyPassport] = []
        for path in sorted(strat_dir.glob("*.json")):
            p = StrictConfigLoader.load_strategy_passport(path)
            if p.enabled:
                passports.append(p)
        return passports

    def load_dataset_for_dates(
        self,
        symbols: List[str],
        start_date: str,
        end_date: str,
        warmup_days: int,
        skip_download: bool = False,
    ) -> Aligned1mDataset:
        sim_start_ms = date_str_to_utc_ms(start_date)
        end_dt = datetime.strptime(end_date.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        sim_end_exclusive_ms = int(end_dt.timestamp() * 1000)
        warmup_start_ms = sim_start_ms - (int(warmup_days) * 86_400_000)

        return self.parser.build_aligned_dataset(
            symbols=symbols,
            warmup_start_ms=warmup_start_ms,
            sim_start_ms=sim_start_ms,
            sim_end_exclusive_ms=sim_end_exclusive_ms,
            skip_download=skip_download,
        )

    def run_tournament(self, skip_download: bool = False) -> List[Dict[str, Any]]:
        passports = self.load_all_passports()
        all_syms = sorted({s for p in passports for s in p.symbols})
        period = self.bt_cfg["period"]
        dataset = self.load_dataset_for_dates(
            symbols=all_syms,
            start_date=str(period["start_date"]),
            end_date=str(period["end_date"]),
            warmup_days=int(period["warmup_days"]),
            skip_download=skip_download,
        )

        summaries = [self.runner.run_passport(p, dataset) for p in passports]
        self._save_reports("tournament_report", period, summaries)
        return summaries

    def run_half_year_study(self, skip_download: bool = False) -> List[Dict[str, Any]]:
        hy_cfg = self.bt_cfg["half_year"]
        strat_dir = self.bot_dir / str(self.app_cfg["paths"]["strategies_dir"])
        passports = [
            StrictConfigLoader.load_strategy_passport(strat_dir / str(name))
            for name in hy_cfg["passports"]
        ]
        all_syms = sorted({s for p in passports for s in p.symbols})
        dataset = self.load_dataset_for_dates(
            symbols=all_syms,
            start_date=str(hy_cfg["start_date"]),
            end_date=str(hy_cfg["end_date"]),
            warmup_days=int(hy_cfg["warmup_days"]),
            skip_download=skip_download,
        )
        summaries = [self.runner.run_passport(p, dataset) for p in passports]
        self._save_reports("halfyear_report", hy_cfg, summaries)
        return summaries

    def run_oos_study(self, base_passport_name: str, skip_download: bool = False) -> Dict[str, Any]:
        strat_dir = self.bot_dir / str(self.app_cfg["paths"]["strategies_dir"])
        base = StrictConfigLoader.load_strategy_passport(strat_dir / base_passport_name)
        oos_cfg = self.bt_cfg["oos_study"]
        warmup_days = int(oos_cfg["warmup_days"])
        metric_key = str(oos_cfg["optimization_metric"])
        n_values = [int(n) for n in oos_cfg["swing_n_candidates"]]

        is_dataset = self.load_dataset_for_dates(
            symbols=base.symbols,
            start_date=str(oos_cfg["in_sample_start_date"]),
            end_date=str(oos_cfg["in_sample_end_date"]),
            warmup_days=warmup_days,
            skip_download=skip_download,
        )

        is_results: List[Dict[str, Any]] = []
        best_n = n_values[0]
        best_score = float("-inf")
        for n in n_values:
            variant = self._clone_with_swing_n(base, n, suffix=f"_IS_N{n}")
            res = self.runner.run_passport(variant, is_dataset)
            is_results.append(res)
            score = float(res[metric_key])
            if score > best_score:
                best_score = score
                best_n = n

        oos_dataset = self.load_dataset_for_dates(
            symbols=base.symbols,
            start_date=str(oos_cfg["oos_start_date"]),
            end_date=str(oos_cfg["oos_end_date"]),
            warmup_days=warmup_days,
            skip_download=skip_download,
        )
        oos_variant = self._clone_with_swing_n(base, best_n, suffix=f"_OOS_N{best_n}")
        oos_result = self.runner.run_passport(oos_variant, oos_dataset)

        payload = {
            "base_strategy": base.strategy_id,
            "optimization_metric": metric_key,
            "best_swing_n": best_n,
            "in_sample_results": is_results,
            "oos_result": oos_result,
        }
        ReportFormatter.export_json(self.reports_dir / "oos_study_report.json", payload)
        return payload

    @staticmethod
    def _clone_with_swing_n(base: StrategyPassport, n: int, suffix: str) -> StrategyPassport:
        new_params = dict(base.signal.params)
        new_params["left_candles"] = n
        new_params["right_candles"] = n
        new_sig = dataclasses.replace(base.signal, params=new_params)
        return dataclasses.replace(base, strategy_id=f"{base.strategy_id}{suffix}", signal=new_sig)

    def _save_reports(self, stem: str, meta: Dict[str, Any], summaries: List[Dict[str, Any]]) -> None:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        ReportFormatter.export_json(
            self.reports_dir / f"{stem}.json",
            {"period": meta, "strategies": summaries},
        )
        ReportFormatter.export_csv(self.reports_dir / f"{stem}.csv", summaries)
        md_text = f"# {stem}\n\n" + ReportFormatter.format_markdown_table(summaries) + "\n"
        (self.reports_dir / f"{stem}.md").write_text(md_text, encoding="utf-8")
