# Path: MarioBot/tests/test_notes_strategies.py
# Role: Юнит-тесты выживших стратегий из notes.md (Bollinger Breakout и EMA Pullback + Trail)

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from MarioBot.CORE.config_loader import StrictConfigLoader
from MarioBot.CORE.models import Candle
from MarioBot.CORE.orchestrator import TradeEngineOrchestrator
from MarioBot.CORE.spec_manager import SpecManager
from MarioBot.CORE.STRATEGY.signal_registry import SignalPluginRegistry
from MarioBacktest.ENGINE.sim_broker import SimExecutionBroker


def _c(idx: int, o: float, h: float, l: float, c: float, sym: str = "ETHUSDT") -> Candle:
    t0 = 1_769_904_000_000 + idx * 14_400_000
    return Candle(sym, t0, t0 + 14_400_000, o, h, l, c, 100.0)


class TestNotesStrategies(unittest.TestCase):
    def setUp(self) -> None:
        self.app_cfg = StrictConfigLoader.read_json(ROOT_DIR / "MarioBot" / "CFG" / "app.json")
        self.spec_mgr = SpecManager.from_app_config(ROOT_DIR / "MarioBot", self.app_cfg)
        self.art_dir = ROOT_DIR / "MarioBot" / "CFG" / "artifacts"

    def test_bollinger_breakout_plugin_triggers_with_swing_sl(self) -> None:
        passport = StrictConfigLoader.load_strategy_passport(self.art_dir / "bb_breakout_4h_markov.json")
        plugin = SignalPluginRegistry.create(passport)
        sig = None
        for i in range(25):
            sig = plugin.update_and_evaluate("ETHUSDT", _c(i, 100.0, 100.5, 99.5, 100.0 + (i % 2) * 0.2))
        self.assertIsNone(sig)

        sig = plugin.update_and_evaluate("ETHUSDT", _c(25, 100.2, 111.0, 100.0, 110.0))
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.side, "LONG")
        self.assertAlmostEqual(sig.origin_extreme_price, 99.5)  # type: ignore[arg-type]

    def test_ema_pullback_and_trailing_signal_exit(self) -> None:
        passport = StrictConfigLoader.load_strategy_passport(self.art_dir / "ema_pullback_4h.json")
        broker = SimExecutionBroker(1000.0, 5, 0.0005, 0.0002, 0.0001, 0.0)
        orch = TradeEngineOrchestrator(passport, self.spec_mgr, broker)
        orch.set_trading_enabled(False)

        for i in range(60):
            px = 100.0 + i * 0.5
            orch.on_candle_close("ETHUSDT", _c(i, px - 0.3, px + 0.4, px - 0.5, px))

        orch.set_trading_enabled(True)
        orch.on_candle_close("ETHUSDT", _c(60, 129.0, 129.5, 123.0, 124.0))
        plan = orch.on_candle_close("ETHUSDT", _c(61, 124.0, 132.0, 123.8, 131.0))
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.side, "LONG")
        self.assertIsNotNone(plan.stop_loss)
        self.assertIsNone(plan.take_profit)

        broker.fill_pending_at_minute_open("ETHUSDT", _c(62, 131.0, 131.0, 131.0, 131.0).open_ts_ms, 131.0)
        self.assertEqual(broker.get_open_position_side("ETHUSDT"), "LONG")

        ema50_val = orch.signal_plugin._states["ETHUSDT"].ema_trail.value  # type: ignore[attr-defined]
        assert ema50_val is not None
        crash_close = ema50_val - 0.5
        orch.on_candle_close("ETHUSDT", _c(62, 131.0, 131.0, crash_close, crash_close))
        broker.fill_pending_at_minute_open("ETHUSDT", _c(63, crash_close, crash_close, crash_close, crash_close).open_ts_ms, crash_close)
        self.assertEqual(len(broker.closed_trades), 1)
        self.assertEqual(broker.closed_trades[0].exit_reason, "SIGNAL_EXIT")


if __name__ == "__main__":
    unittest.main()
