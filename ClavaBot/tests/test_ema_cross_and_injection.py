# Path: ClavaBot/tests/test_ema_cross_and_injection.py
# Role: Тесты EMACrossCalculator, SignalPluginRegistry и реверсивного жизненного цикла

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Optional

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ClavaBot.CORE.config_loader import StrictConfigLoader
from ClavaBot.CORE.models import Candle, SignalEvent
from ClavaBot.CORE.orchestrator import TradeEngineOrchestrator
from ClavaBot.CORE.spec_manager import SpecManager
from ClavaBot.CORE.STRATEGY.base_signal_plugin import BaseSignalPlugin
from ClavaBot.CORE.STRATEGY.ema_cross_signal_plugin import EMACrossCalculator
from ClavaBot.CORE.STRATEGY.ema_trend_filter import EmaTrendFilter
from ClavaBot.CORE.STRATEGY.signal_registry import SignalPluginRegistry
from ClavaBot.CORE.config_loader import EmaTrendFilterConfig
from ClavaBacktest.ENGINE.sim_broker import SimExecutionBroker


class _StubTogglePlugin(BaseSignalPlugin):
    """Тестовый инжектируемый плагин: бар 0 -> LONG, бар 1 -> SHORT (разворот)."""

    def __init__(self) -> None:
        self._step = 0

    def update_and_evaluate(self, symbol: str, candle: Candle) -> Optional[SignalEvent]:
        side = "LONG" if self._step == 0 else "SHORT"
        self._step += 1
        return SignalEvent(symbol=symbol, side=side, signal_ts_ms=candle.close_ts_ms, entry_ref_price=candle.close)


class TestEmaCrossAndInjection(unittest.TestCase):
    def test_ema_cross_calculator_strict_init_and_cross_up_down(self) -> None:
        with self.assertRaises(KeyError):
            EMACrossCalculator({"is_active": True})

        calc = EMACrossCalculator({
            "is_active": True,
            "timeframe": "4h",
            "period1": 7,
            "period2": 25,
            "long_cond": "CROSS_UP",
            "short_cond": "CROSS_DOWN",
        })
        self.assertEqual(calc.calculate([100.0] * 10), ["UNSTABLE"])

        # Падение, затем ускоряющийся разворот вверх -> CROSS_UP
        down = [120.0 - i * 0.5 for i in range(35)]
        up_turn = down + [102.0 + (j ** 1.6) for j in range(1, 15)]
        got_up = False
        for k in range(30, len(up_turn) + 1):
            res = calc.calculate(up_turn[:k])
            if "CROSS_UP" in res:
                got_up = True
        self.assertTrue(got_up)

        # Рост, затем ускоряющийся разворот вниз -> CROSS_DOWN
        up = [80.0 + i * 0.5 for i in range(35)]
        down_turn = up + [98.0 - (j ** 1.6) for j in range(1, 15)]
        got_down = False
        for k in range(30, len(down_turn) + 1):
            res = calc.calculate(down_turn[:k])
            if "CROSS_DOWN" in res:
                got_down = True
        self.assertTrue(got_down)

    def test_reversal_on_opposite_signal_with_injected_plugin(self) -> None:
        app_cfg = StrictConfigLoader.read_json(ROOT_DIR / "ClavaBot" / "CFG" / "app.json")
        passport = StrictConfigLoader.load_strategy_passport(
            ROOT_DIR / "ClavaBot" / "CFG" / "artifacts" / "ema_cross_7_25_4h_markov.json"
        )
        spec_mgr = SpecManager.from_app_config(ROOT_DIR / "ClavaBot", app_cfg)
        broker = SimExecutionBroker(
            start_balance_usd=1000.0,
            leverage=5,
            taker_fee_rate=0.0005,
            maker_fee_rate=0.0002,
            slippage_rate=0.0001,
            tp_slippage_rate=0.0,
        )
        orch = TradeEngineOrchestrator(
            passport=passport,
            spec_manager=spec_mgr,
            broker=broker,
            signal_plugin=_StubTogglePlugin(),
        )
        self.assertIsNotNone(SignalPluginRegistry.create(passport))

        t0 = 1_769_904_000_000
        c1 = Candle("ADAUSDT", t0, t0 + 14_400_000, 0.5, 0.52, 0.49, 0.51, 100.0)
        plan1 = orch.on_candle_close("ADAUSDT", c1)
        self.assertIsNotNone(plan1)
        broker.fill_pending_at_minute_open("ADAUSDT", t0 + 14_400_000, 0.51)
        self.assertEqual(broker.get_open_position_side("ADAUSDT"), "LONG")

        # Вторая свеча -> противоположный SHORT сигнал закрывает LONG и открывает SHORT
        c2 = Candle("ADAUSDT", t0 + 14_400_000, t0 + 28_800_000, 0.51, 0.55, 0.50, 0.54, 100.0)
        plan2 = orch.on_candle_close("ADAUSDT", c2)
        self.assertIsNotNone(plan2)
        broker.fill_pending_at_minute_open("ADAUSDT", t0 + 28_800_000, 0.54)

        self.assertEqual(len(broker.closed_trades), 1)
        self.assertEqual(broker.closed_trades[0].exit_reason, "OPPOSITE_SIGNAL")
        self.assertEqual(broker.closed_trades[0].side, "LONG")
        self.assertEqual(broker.get_open_position_side("ADAUSDT"), "SHORT")

    def test_ema_trend_filter_blocks_counter_entry_but_allows_reversal_close(self) -> None:
        flt = EmaTrendFilter(EmaTrendFilterConfig(enabled=True, period=5))
        t0 = 1_769_904_000_000
        # 5 свечей по 100.0 -> EMA(5) = 100.0; 6-я свеча 110.0 -> close > EMA(5)
        for i, px in enumerate([100.0, 100.0, 100.0, 100.0, 100.0, 110.0]):
            flt.update("ADAUSDT", Candle("ADAUSDT", t0 + i * 1000, t0 + (i + 1) * 1000, px, px, px, px, 10.0))

        self.assertTrue(flt.is_allowed("ADAUSDT", "LONG"))
        self.assertFalse(flt.is_allowed("ADAUSDT", "SHORT"))

        # Проверка в оркестраторе: открыт LONG выше EMA200; при CROSS_DOWN выше EMA200 LONG закрывается, а SHORT НЕ открывается
        app_cfg = StrictConfigLoader.read_json(ROOT_DIR / "ClavaBot" / "CFG" / "app.json")
        passport = StrictConfigLoader.load_strategy_passport(
            ROOT_DIR / "ClavaBot" / "CFG" / "artifacts" / "ema_cross_9_21_ema200_4h.json"
        )
        # Используем период 5 для компактного теста в памяти
        import dataclasses
        passport = dataclasses.replace(passport, ema_trend_filter=EmaTrendFilterConfig(enabled=True, period=5))
        broker = SimExecutionBroker(1000.0, 5, 0.0005, 0.0002, 0.0001, 0.0)
        orch = TradeEngineOrchestrator(passport, SpecManager.from_app_config(ROOT_DIR / "ClavaBot", app_cfg), broker, signal_plugin=_StubTogglePlugin())
        orch.set_trading_enabled(False)
        for i in range(5):
            orch.on_candle_close("ADAUSDT", Candle("ADAUSDT", t0 + i * 1000, t0 + (i + 1) * 1000, 100.0, 100.0, 100.0, 100.0, 10.0))
        orch.set_trading_enabled(True)
        # Пересоздаем стаб с шага 0 (step 0 -> LONG, step 1 -> SHORT)
        orch.signal_plugin = _StubTogglePlugin()

        # Бар 1 (110 > EMA5) -> открывает LONG
        plan1 = orch.on_candle_close("ADAUSDT", Candle("ADAUSDT", t0 + 5000, t0 + 6000, 110.0, 110.0, 110.0, 110.0, 10.0))
        self.assertIsNotNone(plan1)
        broker.fill_pending_at_minute_open("ADAUSDT", t0 + 6000, 110.0)
        self.assertEqual(broker.get_open_position_side("ADAUSDT"), "LONG")

        # Бар 2 (108 всё ещё > EMA5) -> противоположный SHORT закрывает LONG, но новый SHORT заблокирован фильтром EMA!
        plan2 = orch.on_candle_close("ADAUSDT", Candle("ADAUSDT", t0 + 6000, t0 + 7000, 108.0, 108.0, 108.0, 108.0, 10.0))
        self.assertIsNone(plan2)
        broker.fill_pending_at_minute_open("ADAUSDT", t0 + 7000, 108.0)
        self.assertEqual(len(broker.closed_trades), 1)
        self.assertEqual(broker.closed_trades[0].exit_reason, "OPPOSITE_SIGNAL")
        self.assertIsNone(broker.get_open_position_side("ADAUSDT"))


if __name__ == "__main__":
    unittest.main()
