# Path: MarioBot/tests/test_top15_compounding_passport.py
# Role: Проверка боевого паспорта «Альфа» (14 монет, $1,000, режимы reinvest/fixed без плеча, same_side=5, open_risk=10%)

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path
from typing import Dict

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from MarioBot.CORE.config_loader import CapitalConfig, StrictConfigLoader
from MarioBot.CORE.EXECUTION.mock_live_broker import MockLiveExecutionBroker
from MarioBot.CORE.models import Candle, SignalEvent
from MarioBot.CORE.orchestrator import TradeEngineOrchestrator
from MarioBot.CORE.spec_manager import SpecManager
from MarioBot.CORE.STRATEGY.base_signal_plugin import BaseSignalPlugin
from MarioBot.CORE.STRATEGY.position_sizer import PositionSizer
from MarioBot.main import build_prod_runtime

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
        app_cfg = StrictConfigLoader.read_json(ROOT_DIR / "MarioBot" / "CFG" / "app.json")
        passport = StrictConfigLoader.load_primary_slot_passport(ROOT_DIR / "MarioBot", app_cfg)

        self.assertEqual(passport.strategy_id, "markov_alpha_top14_main")
        self.assertEqual(passport.symbols, EXPECTED_TOP14)
        self.assertNotIn("DOTUSDT", passport.symbols)
        self.assertEqual(passport.timeframe, "4h")
        self.assertAlmostEqual(passport.capital.start_balance_usd, 1000.0)
        self.assertEqual(passport.capital.sizing_mode, "reinvest")
        self.assertEqual(passport.capital.leverage, 20)
        self.assertEqual(passport.capital.max_open_positions, 10)
        self.assertAlmostEqual(passport.capital.reinvest_notional_equity_ratio or 0.0, 0.12)
        self.assertAlmostEqual(passport.capital.fixed_notional_usd or 0.0, 120.0)
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

    def test_reinvest_and_fixed_sizing_ignore_leverage_completely(self) -> None:
        app_cfg = StrictConfigLoader.read_json(ROOT_DIR / "MarioBot" / "CFG" / "app.json")
        spec_mgr = SpecManager.from_app_config(ROOT_DIR / "MarioBot", app_cfg)

        # Режим REINVEST (12% от equity): leverage_display=1 vs 100 даёт 100% идентичный объём!
        reinv_lev1 = PositionSizer(
            CapitalConfig(1000.0, 1, 10, None, 120.0, "reinvest", 0.12, True, True), spec_mgr
        )
        reinv_lev100 = PositionSizer(
            CapitalConfig(1000.0, 100, 10, None, 120.0, "reinvest", 0.12, True, True), spec_mgr
        )
        # $1,000 * 12% = $120 -> 0.60 SOL по $200; при equity $2,000 -> 1.20 SOL
        self.assertAlmostEqual(reinv_lev1.calculate_qty("SOLUSDT", 1000.0, 100.0, 200.0, 180.0, 1.0), 0.60, places=2)
        self.assertAlmostEqual(reinv_lev100.calculate_qty("SOLUSDT", 1000.0, 100.0, 200.0, 180.0, 1.0), 0.60, places=2)
        self.assertAlmostEqual(reinv_lev1.calculate_qty("SOLUSDT", 2000.0, 100.0, 200.0, 180.0, 1.0), 1.20, places=2)

        # Режим FIXED ($120): не зависит ни от equity ($1k vs $50k), ни от плеча!
        fixed_sz = PositionSizer(
            CapitalConfig(1000.0, 20, 10, None, 120.0, "fixed", 0.12, True, False), spec_mgr
        )
        self.assertAlmostEqual(fixed_sz.calculate_qty("SOLUSDT", 1000.0, 100.0, 200.0, 180.0, 1.5), 0.60, places=2)
        self.assertAlmostEqual(fixed_sz.calculate_qty("SOLUSDT", 50000.0, 100.0, 200.0, 180.0, 1.5), 0.60, places=2)

    def test_compounding_reinvest_and_portfolio_caps(self) -> None:
        app_cfg = StrictConfigLoader.read_json(ROOT_DIR / "MarioBot" / "CFG" / "app.json")
        passport = StrictConfigLoader.load_primary_slot_passport(ROOT_DIR / "MarioBot", app_cfg)
        passport = dataclasses.replace(
            passport,
            markov_regime=dataclasses.replace(passport.markov_regime, enabled=False),
            market_sentinel=dataclasses.replace(passport.market_sentinel, enabled=False),
        )
        spec_mgr = SpecManager.from_app_config(ROOT_DIR / "MarioBot", app_cfg)
        broker = MockLiveExecutionBroker(start_balance_usd=1000.0, leverage=20, taker_fee_rate=0.0005)
        orch = TradeEngineOrchestrator(
            passport=passport,
            spec_manager=spec_mgr,
            broker=broker,
            signal_plugin=_AllBurstSignalPlugin(),
        )

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

        # 5 LONG (same_side=5) + 4 SHORT (при 12% сайзе и 10% стопе риск 1 сделки = 1.2% -> 9 сделок не превышают 10% до 9-й -> 8 сделок!)
        self.assertEqual(sum(1 for p in broker.positions.values() if p.side == "LONG"), 5)
        self.assertGreaterEqual(opened, 7)
        self.assertLessEqual(opened, 9)

    def test_three_new_exit_mechanics_unit(self) -> None:
        from MarioBot.CORE.models import OpenPosition
        from MarioBot.CORE.STRATEGY.markov_pure_signal_plugin import MarkovPureSignalPlugin

        app_cfg = StrictConfigLoader.read_json(ROOT_DIR / "MarioBot" / "CFG" / "app.json")
        slot_cfg = app_cfg["active_strategies"]["markov_alpha_top14_main"]
        strat_dir = ROOT_DIR / "MarioBot" / "CFG" / "strategies"
        art_dir = ROOT_DIR / "MarioBot" / "CFG" / "artifacts"

        # 1. V3 MaxHold 14d (в artifacts): на 13-й день держит, на 14-й закрывает
        p_v3 = StrictConfigLoader.load_strategy_passport(
            art_dir / "markov_pure_4h_gmm_conf_antichop_exit_sw_sl5_v3_maxhold14d.json", slot_cfg=slot_cfg
        )
        pl_v3 = MarkovPureSignalPlugin(p_v3)
        pos = OpenPosition("SOLUSDT", "LONG", 1.0, 100.0, 1_700_000_000_000, 80.0, None, 0.0, 0.0)
        c13 = Candle("SOLUSDT", pos.entry_ts_ms + 13 * 86_400_000 - 14_400_000, pos.entry_ts_ms + 13 * 86_400_000, 102.0, 103.0, 101.0, 102.0, 100.0)
        c14 = Candle("SOLUSDT", pos.entry_ts_ms + 14 * 86_400_000 - 14_400_000, pos.entry_ts_ms + 14 * 86_400_000, 102.0, 103.0, 101.0, 102.0, 100.0)
        self.assertIsNone(pl_v3.check_exit("SOLUSDT", c13, pos))
        self.assertEqual(pl_v3.check_exit("SOLUSDT", c14, pos), "SIGNAL_EXIT")

        # 2. V1 Stale Drift (в artifacts): прогреваем ATR=2.0 -> на 8д при +1.0 (0.5*ATR) закрывает, при +5.0 (2.5*ATR) держит
        p_v1 = StrictConfigLoader.load_strategy_passport(
            art_dir / "markov_pure_4h_gmm_conf_antichop_exit_sw_sl5_v1_stale_drift.json", slot_cfg=slot_cfg
        )
        pl_v1 = MarkovPureSignalPlugin(p_v1)
        for i in range(20):
            pl_v1._atrs["SOLUSDT"].update(Candle("SOLUSDT", i * 14_400_000, (i + 1) * 14_400_000, 100.0, 102.0, 100.0, 101.0, 10.0))
        c8_drift = Candle("SOLUSDT", pos.entry_ts_ms + 8 * 86_400_000 - 14_400_000, pos.entry_ts_ms + 8 * 86_400_000, 101.0, 101.5, 100.5, 101.0, 10.0)
        c8_trend = Candle("SOLUSDT", pos.entry_ts_ms + 8 * 86_400_000 - 14_400_000, pos.entry_ts_ms + 8 * 86_400_000, 105.0, 106.0, 104.5, 105.0, 10.0)
        self.assertIsNone(pl_v1.check_exit("SOLUSDT", c8_trend, pos))
        self.assertEqual(pl_v1.check_exit("SOLUSDT", c8_drift, pos), "SIGNAL_EXIT")

        # 3. V2 Profit-Activated Trail (+10*ATR -> trail 3.5*ATR): при ATR=2.0 активация на 120.0, откат ниже 125 - 7 = 118 -> выход
        p_v2 = StrictConfigLoader.load_strategy_passport(
            strat_dir / "markov_pure_4h_gmm_conf_antichop_exit_sw_sl5_v2_profit_trail10.json", slot_cfg=slot_cfg
        )
        pl_v2 = MarkovPureSignalPlugin(p_v2)
        for i in range(20):
            pl_v2._atrs["SOLUSDT"].update(Candle("SOLUSDT", i * 14_400_000, (i + 1) * 14_400_000, 100.0, 102.0, 100.0, 101.0, 10.0))
        c_arm = Candle("SOLUSDT", pos.entry_ts_ms + 14_400_000, pos.entry_ts_ms + 28_800_000, 118.0, 125.0, 118.0, 124.0, 10.0)
        c_hit = Candle("SOLUSDT", pos.entry_ts_ms + 28_800_000, pos.entry_ts_ms + 43_200_000, 123.0, 123.0, 117.0, 117.5, 10.0)
        self.assertIsNone(pl_v2.check_exit("SOLUSDT", c_arm, pos))
        self.assertEqual(pl_v2.check_exit("SOLUSDT", c_hit, pos), "SIGNAL_EXIT")


if __name__ == "__main__":
    unittest.main()
