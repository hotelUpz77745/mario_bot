# Path: ClavaBot/tests/test_orchestrator_and_analytics.py
# Role: Тесты мультисимвольного оркестратора и блока аналитики (Winrate, ROI, Calmar, Sharpe, MaxDD)

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ClavaBot.ANALYTICS.equity_tracker import EquityTracker
from ClavaBot.ANALYTICS.metrics_engine import AnalyticsMetricsEngine
from ClavaBot.ANALYTICS.report_formatter import ReportFormatter
from ClavaBot.CORE.EXECUTION.mock_live_broker import MockLiveExecutionBroker
from ClavaBot.CORE.STRATEGY.choch_signal_plugin import ChochSignalPlugin
from ClavaBot.CORE.config_loader import StrictConfigLoader
from ClavaBot.CORE.models import Candle, ClosedTrade
from ClavaBot.CORE.orchestrator import TradeEngineOrchestrator
from ClavaBot.CORE.spec_manager import SpecManager


class TestOrchestratorAndAnalytics(unittest.TestCase):
    def test_orchestrator_initializes_multi_symbol_and_respects_trading_flag(self) -> None:
        app_cfg = StrictConfigLoader.read_json(ROOT_DIR / "ClavaBot" / "CFG" / "app.json")
        passport = StrictConfigLoader.load_strategy_passport(
            ROOT_DIR / "ClavaBot" / "CFG" / "artifacts" / "choch_4h_weekdays_ny_markov_gate.json"
        )
        spec_mgr = SpecManager(app_cfg["symbol_specs"])
        broker = MockLiveExecutionBroker(start_balance_usd=1000.0, leverage=5, taker_fee_rate=0.0005)
        orch = TradeEngineOrchestrator(passport=passport, spec_manager=spec_mgr, broker=broker)

        self.assertIsInstance(orch.signal_plugin, ChochSignalPlugin)
        assert isinstance(orch.signal_plugin, ChochSignalPlugin)
        self.assertEqual(len(orch.signal_plugin.swing_detectors), 6)
        orch.set_trading_enabled(False)
        res = orch.on_candle_close(
            "ADAUSDT",
            Candle("ADAUSDT", 1_700_000_000_000, 1_700_014_400_000, 0.5, 0.52, 0.49, 0.51, 1000.0),
        )
        self.assertIsNone(res)

    def test_analytics_metrics_all_required_fields(self) -> None:
        tracker = EquityTracker(start_balance_usd=1000.0)
        t0 = 1_700_000_000_000
        day = 86_400_000
        for idx, eq in enumerate([1000.0, 1050.0, 980.0, 1120.0, 1200.0]):
            tracker.record(t0 + idx * day, eq, record_curve_point=True)

        trades = [
            ClosedTrade("ETHUSDT", "LONG", 0.1, 2000.0, 2100.0, t0, t0 + day, 1900.0, 2100.0, 10.0, 0.4, 9.6, "TAKE_PROFIT", 40.0),
            ClosedTrade("SOLUSDT", "SHORT", 1.0, 150.0, 155.0, t0 + day, t0 + 2 * day, 155.0, 145.0, -5.0, 0.3, -5.3, "STOP_LOSS", 30.0),
            ClosedTrade("ADAUSDT", "LONG", 100.0, 0.5, 0.6, t0 + 2 * day, t0 + 3 * day, 0.4, 0.6, 10.0, 0.1, 9.9, "TAKE_PROFIT", 10.0),
        ]
        summary = AnalyticsMetricsEngine.compute(
            strategy_id="test_strat",
            start_balance_usd=1000.0,
            end_equity_usd=1200.0,
            trades=trades,
            tracker=tracker,
            sim_start_ms=t0,
            sim_end_ms=t0 + 5 * day,
            annualization_days=365,
        )
        self.assertAlmostEqual(summary["net_pnl_usd"], 200.0)
        self.assertAlmostEqual(summary["roi_pct"], 20.0)
        self.assertAlmostEqual(summary["winrate_pct"], 66.67)
        self.assertAlmostEqual(summary["max_drawdown_usd"], 70.0)
        self.assertGreater(summary["calmar_ratio"], 0.0)
        self.assertGreater(summary["sharpe_ratio"], 0.0)
        self.assertIn("ETHUSDT", summary["per_coin"])

        ascii_tbl = ReportFormatter.format_ascii_table([summary])
        self.assertIn("test_strat", ascii_tbl)
        self.assertIn("Calmar", ascii_tbl)
        self.assertIn("Sharpe", ascii_tbl)


if __name__ == "__main__":
    unittest.main()
