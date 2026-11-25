# Path: ClavaBot/tests/test_backtest_engine.py
# Role: Тесты 1-минутного симулятора (агрегация 4H, пессимистичный SL раньше TP, BacktestRunner)

from __future__ import annotations

import sys
import unittest
from pathlib import Path
import numpy as np

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ClavaBot.CORE.candle_aggregator import CandleAggregator
from ClavaBot.CORE.config_loader import StrictConfigLoader
from ClavaBot.CORE.models import OrderPlan
from ClavaBot.CORE.spec_manager import SpecManager
from ClavaBacktest.ENGINE.backtest_runner import BacktestRunner
from ClavaBacktest.ENGINE.sim_broker import SimExecutionBroker
from ClavaBacktest.ENGINE.vision_parser import Aligned1mDataset


class TestBacktestEngine(unittest.TestCase):
    def test_pessimistic_sl_before_tp_on_same_1m_candle(self) -> None:
        broker = SimExecutionBroker(
            start_balance_usd=1000.0,
            leverage=5,
            taker_fee_rate=0.0005,
            maker_fee_rate=0.0002,
            slippage_rate=0.0001,
            tp_slippage_rate=0.0,
        )
        plan = OrderPlan(
            symbol="ETHUSDT",
            side="LONG",
            qty=0.1,
            entry_ref_price=2000.0,
            stop_loss=1980.0,
            take_profit=2020.0,
            signal_ts_ms=1_700_000_000_000,
            notional_usd=200.0,
        )
        self.assertTrue(broker.submit_market_with_bracket(plan))
        broker.fill_pending_at_minute_open("ETHUSDT", 1_700_000_060_000, 2000.0)

        # Одна 1m свеча задевает И SL (low=1975 <= 1980), И TP (high=2030 >= 2020)
        broker.process_1m_bar("ETHUSDT", 1_700_000_060_000, 2000.0, 2030.0, 1975.0, 2010.0)
        self.assertEqual(len(broker.closed_trades), 1)
        self.assertEqual(broker.closed_trades[0].exit_reason, "STOP_LOSS")
        self.assertLess(broker.closed_trades[0].net_pnl_usd, 0.0)

    def test_candle_aggregator_4h_boundaries(self) -> None:
        agg = CandleAggregator("4h")
        t0 = 1_704_067_200_000  # 2024-01-01 00:00:00 UTC
        agg.push_1m("ETHUSDT", t0, 2000.0, 2010.0, 1995.0, 2005.0, 10.0)
        self.assertIsNone(agg.flush_if_closed("ETHUSDT", t0 + 60_000))
        agg.push_1m("ETHUSDT", t0 + 60_000, 2005.0, 2025.0, 1990.0, 2015.0, 15.0)

        # Следующий 4H бакет (04:00 UTC) закрывает предыдущий 4H-бар
        closed = agg.flush_if_closed("ETHUSDT", t0 + 14_400_000)
        self.assertIsNotNone(closed)
        assert closed is not None
        self.assertEqual(closed.open, 2000.0)
        self.assertEqual(closed.high, 2025.0)
        self.assertEqual(closed.low, 1990.0)
        self.assertEqual(closed.close, 2015.0)
        self.assertEqual(closed.volume, 25.0)

    def test_backtest_runner_synthetic_1m_dataset(self) -> None:
        app_cfg = StrictConfigLoader.read_json(ROOT_DIR / "ClavaBot" / "CFG" / "app.json")
        bt_cfg = StrictConfigLoader.read_json(ROOT_DIR / "ClavaBacktest" / "CFG" / "backtest.json")
        passport = StrictConfigLoader.load_strategy_passport(
            ROOT_DIR / "ClavaBot" / "CFG" / "artifacts" / "choch_4h_weekdays_ny_markov_gate.json"
        )

        # Синтетический 1m поток: 40 баров 4H прогрева + 60 баров 4H симуляции (по 240 минут, шаг 240m -> упростим через 1m шаг 14400с нельзя, делаем по минутам за 10 дней)
        w_mins = 5 * 1440
        s_mins = 10 * 1440
        t0 = 1_769_904_000_000  # 2026-02-01 00:00:00 UTC
        w_ts = np.arange(t0 - w_mins * 60_000, t0, 60_000, dtype=np.int64)
        s_ts = np.arange(t0, t0 + s_mins * 60_000, 60_000, dtype=np.int64)

        n_syms = len(passport.symbols)
        w_ohlcv = np.zeros((w_mins, n_syms, 5), dtype=np.float64)
        s_ohlcv = np.zeros((s_mins, n_syms, 5), dtype=np.float64)
        for idx in range(n_syms):
            w_prices = 100.0 + np.sin(np.arange(w_mins) / 180.0 + idx) * 3.0
            s_prices = 100.0 + np.sin(np.arange(s_mins) / 160.0 + idx) * 6.0
            for arr, px in ((w_ohlcv[:, idx, :], w_prices), (s_ohlcv[:, idx, :], s_prices)):
                arr[:, 0] = px
                arr[:, 1] = px + 0.4
                arr[:, 2] = px - 0.4
                arr[:, 3] = px
                arr[:, 4] = 10.0

        dataset = Aligned1mDataset(
            symbols=list(passport.symbols),
            warmup_ts_ms=w_ts,
            warmup_ohlcv=w_ohlcv,
            warmup_valid=np.ones((w_mins, n_syms), dtype=bool),
            sim_ts_ms=s_ts,
            sim_ohlcv=s_ohlcv,
            sim_valid=np.ones((s_mins, n_syms), dtype=bool),
        )

        runner = BacktestRunner(SpecManager.from_app_config(ROOT_DIR / "ClavaBot", app_cfg), bt_cfg["execution"])
        res = runner.run_passport(passport, dataset)
        self.assertEqual(res["strategy_id"], "choch_4h_weekdays_ny_markov_gate")
        self.assertIn("calmar_ratio", res)
        self.assertIn("sharpe_ratio", res)


if __name__ == "__main__":
    unittest.main()
