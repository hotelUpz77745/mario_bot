# Path: ClavaBot/tests/test_top15_compounding_passport.py
# Role: Проверка канонического боевого паспорта «Альфа» (14 монет, $1,000, risk=1.5%, same_side=5, open_risk=10%)

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path
from typing import Dict

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ClavaBot.CORE.config_loader import StrictConfigLoader
from ClavaBot.CORE.EXECUTION.mock_live_broker import MockLiveExecutionBroker
from ClavaBot.CORE.models import Candle, SignalEvent
from ClavaBot.CORE.orchestrator import TradeEngineOrchestrator
from ClavaBot.CORE.spec_manager import SpecManager
from ClavaBot.CORE.STRATEGY.base_signal_plugin import BaseSignalPlugin
from ClavaBot.main import build_prod_runtime

PASSPORT_FILENAME = "markov_pure_4h_gmm_conf_antichop_exit_sw_sl5_top14_r15.json"

EXPECTED_TOP14 = [
    "ADAUSDT",
    "SOLUSDT",
    "AVAXUSDT",
    "DOGEUSDT",
    "1000SHIBUSDT",
    "HBARUSDT",
    "SUIUSDT",
    "UNIUSDT",
    "XRPUSDT",
    "BCHUSDT",
    "BNBUSDT",
    "ETHUSDT",
    "LINKUSDT",
    "XLMUSDT",
]


class _AllBurstSignalPlugin(BaseSignalPlugin):
    """Первые 8 монет дают LONG, остальные 6 — SHORT (проверка same_side=5 и open_risk=10%)."""

    def __init__(self) -> None:
        self._fired: Dict[str, bool] = {}

    def update_and_evaluate(self, symbol: str, candle: Candle) -> SignalEvent | None:
        if self._fired.get(symbol):
            return None
        idx = len(self._fired)
        self._fired[symbol] = True
        is_long = idx < 8
        side = "LONG" if is_long else "SHORT"
        sl = candle.close * (0.90 if is_long else 1.10)
        return SignalEvent(
            symbol=symbol,
            side=side,
            signal_ts_ms=candle.close_ts_ms,
            entry_ref_price=candle.close,
            broken_level_price=candle.close,
            broken_level_bar_index=1,
            origin_extreme_price=sl,
            size_multiplier=1.0,
        )


class TestAlpha14CompoundingPassport(unittest.TestCase):
    def test_passport_schema_and_runtime_build(self) -> None:
        passport_path = ROOT_DIR / "ClavaBot" / "CFG" / "strategies" / PASSPORT_FILENAME
        passport = StrictConfigLoader.load_strategy_passport(passport_path)

        self.assertEqual(passport.strategy_id, "markov_pure_4h_gmm_conf_antichop_exit_sw_sl5_top14_r15")
        self.assertEqual(passport.symbols, EXPECTED_TOP14)
        self.assertNotIn("DOTUSDT", passport.symbols)
        self.assertAlmostEqual(passport.capital.start_balance_usd, 1000.0)
        self.assertEqual(passport.capital.leverage, 2)
        self.assertEqual(passport.capital.max_open_positions, 10)
        self.assertAlmostEqual(passport.capital.risk_per_trade_ratio or 0.0, 0.015)
        self.assertIsNone(passport.capital.fixed_notional_usd)
        self.assertTrue(bool(passport.signal.params["exit_on_sideways"]))
        self.assertEqual(int(passport.signal.params["recalibrate_interval_days"]), 3)
        self.assertAlmostEqual(float(passport.signal.params["atr_sl_mult"]), 5.0)
        self.assertEqual(passport.market_sentinel.benchmark_symbol, "BTCUSDT")
        self.assertTrue(passport.portfolio_risk.enabled)
        self.assertEqual(passport.portfolio_risk.max_same_side_positions, 5)
        self.assertAlmostEqual(passport.portfolio_risk.max_portfolio_risk_ratio or 0.0, 0.10)
        self.assertTrue(passport.portfolio_risk.funding_guard_enabled)

        orch, _agg = build_prod_runtime()
        for sym in EXPECTED_TOP14 + ["BTCUSDT"]:
            spec = orch.spec_manager.get_spec(sym)
            self.assertGreater(float(spec.tick_size), 0.0)
            self.assertGreater(float(spec.step_size), 0.0)
            self.assertGreater(spec.min_notional_usd, 0.0)

    def test_compounding_risk_per_trade_and_portfolio_caps(self) -> None:
        app_cfg = StrictConfigLoader.read_json(ROOT_DIR / "ClavaBot" / "CFG" / "app.json")
        passport = StrictConfigLoader.load_strategy_passport(
            ROOT_DIR / "ClavaBot" / "CFG" / "strategies" / PASSPORT_FILENAME
        )
        passport = dataclasses.replace(
            passport,
            markov_regime=dataclasses.replace(passport.markov_regime, enabled=False),
            market_sentinel=dataclasses.replace(passport.market_sentinel, enabled=False),
        )
        spec_mgr = SpecManager.from_app_config(ROOT_DIR / "ClavaBot", app_cfg)
        broker = MockLiveExecutionBroker(start_balance_usd=1000.0, leverage=2, taker_fee_rate=0.0005)
        orch = TradeEngineOrchestrator(
            passport=passport,
            spec_manager=spec_mgr,
            broker=broker,
            signal_plugin=_AllBurstSignalPlugin(),
        )

        # $1,000 баланс, риск 1.5% ($15) при 10% стопе -> нотионал $150 (0.75 SOL по $200)
        qty_1k = orch.position_sizer.calculate_qty("SOLUSDT", 1000.0, 1000.0, 200.0, 180.0, 1.0)
        qty_10k = orch.position_sizer.calculate_qty("SOLUSDT", 10000.0, 10000.0, 200.0, 180.0, 1.0)
        self.assertAlmostEqual(qty_1k, 0.75, places=2)
        self.assertAlmostEqual(qty_10k, 7.5, places=2)

        sample_prices = {
            "ADAUSDT": 0.80,
            "SOLUSDT": 200.0,
            "AVAXUSDT": 35.0,
            "DOGEUSDT": 0.35,
            "1000SHIBUSDT": 0.025,
            "HBARUSDT": 0.15,
            "SUIUSDT": 3.5,
            "UNIUSDT": 10.0,
            "XRPUSDT": 1.2,
            "BCHUSDT": 450.0,
            "BNBUSDT": 620.0,
            "ETHUSDT": 3100.0,
            "LINKUSDT": 15.0,
            "XLMUSDT": 0.30,
        }
        t0 = 1_700_000_000_000
        opened = 0
        for sym in EXPECTED_TOP14:
            px = sample_prices[sym]
            plan = orch.on_candle_close(
                sym,
                Candle(sym, t0, t0 + 14_400_000, px * 0.99, px * 1.01, px * 0.98, px, 10000.0),
            )
            if plan is not None:
                opened += 1

        # 5 LONG (same_side=5) + 2 SHORT (до open_risk=10%) = 7 позиций
        self.assertEqual(opened, 7)
        self.assertEqual(sum(1 for p in broker.positions.values() if p.side == "LONG"), 5)
        self.assertEqual(sum(1 for p in broker.positions.values() if p.side == "SHORT"), 2)


if __name__ == "__main__":
    unittest.main()
