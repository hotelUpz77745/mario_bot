# Path: ClavaBot/tests/test_top15_compounding_passport.py
# Role: Проверка боевого паспорта №3 (Top-15 + risk_per_trade_ratio=0.015 + max_open_positions=10)

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path
from typing import Dict

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ClavaBot.CORE.EXECUTION.mock_live_broker import MockLiveExecutionBroker
from ClavaBot.CORE.config_loader import StrictConfigLoader
from ClavaBot.CORE.models import Candle, SignalEvent
from ClavaBot.CORE.orchestrator import TradeEngineOrchestrator
from ClavaBot.CORE.spec_manager import SpecManager
from ClavaBot.CORE.STRATEGY.base_signal_plugin import BaseSignalPlugin
from ClavaBot.main import build_prod_runtime

PASSPORT_FILENAME = "markov_pure_4h_gmm_conf_antichop_exit_sw_sl5_top15_r15.json"

EXPECTED_TOP15 = [
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
    "DOTUSDT",
    "BNBUSDT",
    "ETHUSDT",
    "LINKUSDT",
    "XLMUSDT",
]


class _AllBurstSignalPlugin(BaseSignalPlugin):
    """Тестовый плагин: генерирует LONG-сигнал со стопом на первом баре каждой монеты."""

    def __init__(self) -> None:
        self._fired: Dict[str, bool] = {}

    def update_and_evaluate(self, symbol: str, candle: Candle) -> SignalEvent | None:
        if self._fired.get(symbol):
            return None
        self._fired[symbol] = True
        sl = candle.close * 0.90  # 10% SL дистанция для предсказуемого risk_per_trade_ratio
        return SignalEvent(
            symbol=symbol,
            side="LONG",
            signal_ts_ms=candle.close_ts_ms,
            entry_ref_price=candle.close,
            broken_level_price=candle.close,
            broken_level_bar_index=1,
            origin_extreme_price=sl,
            size_multiplier=1.0,
        )


class TestTop15CompoundingPassport(unittest.TestCase):
    def test_passport_schema_and_runtime_build(self) -> None:
        passport_path = ROOT_DIR / "ClavaBot" / "CFG" / "strategies" / PASSPORT_FILENAME
        passport = StrictConfigLoader.load_strategy_passport(passport_path)

        self.assertEqual(passport.strategy_id, "markov_pure_4h_gmm_conf_antichop_exit_sw_sl5_top15_r15")
        self.assertEqual(passport.symbols, EXPECTED_TOP15)
        self.assertEqual(passport.capital.leverage, 2)
        self.assertEqual(passport.capital.max_open_positions, 10)
        self.assertAlmostEqual(passport.capital.risk_per_trade_ratio or 0.0, 0.015)
        self.assertIsNone(passport.capital.fixed_notional_usd)
        self.assertTrue(bool(passport.signal.params["exit_on_sideways"]))
        self.assertAlmostEqual(float(passport.signal.params["atr_sl_mult"]), 5.0)
        self.assertEqual(passport.market_sentinel.benchmark_symbol, "BTCUSDT")

        # Сборка полного прод-рантайма (проверка, что все 15 монет + BTCUSDT есть в app.json/symbol_specs)
        orch, _agg = build_prod_runtime(PASSPORT_FILENAME)
        for sym in EXPECTED_TOP15 + ["BTCUSDT"]:
            spec = orch.spec_manager.get_spec(sym)
            self.assertGreater(float(spec.tick_size), 0.0)
            self.assertGreater(float(spec.step_size), 0.0)
            self.assertGreater(spec.min_notional_usd, 0.0)

    def test_compounding_risk_per_trade_and_max_10_slots(self) -> None:
        app_cfg = StrictConfigLoader.read_json(ROOT_DIR / "ClavaBot" / "CFG" / "app.json")
        passport = StrictConfigLoader.load_strategy_passport(
            ROOT_DIR / "ClavaBot" / "CFG" / "strategies" / PASSPORT_FILENAME
        )
        passport = dataclasses.replace(
            passport,
            markov_regime=dataclasses.replace(passport.markov_regime, enabled=False),
            market_sentinel=dataclasses.replace(passport.market_sentinel, enabled=False),
        )
        spec_mgr = SpecManager(app_cfg["symbol_specs"])
        broker = MockLiveExecutionBroker(start_balance_usd=10000.0, leverage=2, taker_fee_rate=0.0005)
        orch = TradeEngineOrchestrator(
            passport=passport,
            spec_manager=spec_mgr,
            broker=broker,
            signal_plugin=_AllBurstSignalPlugin(),
        )

        # Проверка масштабирования сайза от equity при risk_per_trade_ratio = 0.015 (SL = 10% -> Notional ~ 15% equity)
        qty_10k = orch.position_sizer.calculate_qty(
            symbol="SOLUSDT",
            equity_usd=10000.0,
            free_margin_usd=10000.0,
            entry_price=200.0,
            stop_loss_price=180.0,  # 10% SL
            size_multiplier=1.0,
        )
        qty_50k = orch.position_sizer.calculate_qty(
            symbol="SOLUSDT",
            equity_usd=50000.0,
            free_margin_usd=50000.0,
            entry_price=200.0,
            stop_loss_price=180.0,
            size_multiplier=1.0,
        )
        # Risk $150 / $20 = 7.5 SOL ($1,500) при $10k и Risk $750 / $20 = 37.5 SOL ($7,500) при $50k
        self.assertAlmostEqual(qty_10k, 7.5, places=2)
        self.assertAlmostEqual(qty_50k, 37.5, places=2)

        # Прогоняем сигналы по всем 15 монетам: должно открыться ровно 10 позиций (max_open_positions = 10)
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
            "DOTUSDT": 7.0,
            "BNBUSDT": 620.0,
            "ETHUSDT": 3100.0,
            "LINKUSDT": 15.0,
            "XLMUSDT": 0.30,
        }
        t0 = 1_700_000_000_000
        opened = 0
        for i, sym in enumerate(EXPECTED_TOP15):
            px = sample_prices[sym]
            plan = orch.on_candle_close(
                sym,
                Candle(sym, t0, t0 + 14_400_000, px * 0.99, px * 1.01, px * 0.98, px, 10000.0),
            )
            if plan is not None:
                opened += 1

        self.assertEqual(opened, 10)
        self.assertEqual(broker.open_positions_count(), 10)


if __name__ == "__main__":
    unittest.main()
