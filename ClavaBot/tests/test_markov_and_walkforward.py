# Path: ClavaBot/tests/test_markov_and_walkforward.py
# Role: Юнит-тесты MarkovRegimeFilter и WalkForwardValidator

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ClavaBot.CORE.config_loader import MarkovRegimeConfig
from ClavaBot.CORE.models import Candle
from ClavaBot.CORE.STRATEGY.markov_regime_filter import MarkovRegimeFilter


def _daily_4h_series(closes: list[float], symbol: str = "ADAUSDT") -> list[Candle]:
    bars: list[Candle] = []
    t0 = 1_704_067_200_000  # 2024-01-01 00:00 UTC
    for d_idx, px in enumerate(closes):
        day_start = t0 + d_idx * 86_400_000
        for sub in range(6):
            open_ms = day_start + sub * 14_400_000
            bars.append(
                Candle(
                    symbol=symbol,
                    open_ts_ms=open_ms,
                    close_ts_ms=open_ms + 14_400_000,
                    open=px,
                    high=px * 1.01,
                    low=px * 0.99,
                    close=px,
                    volume=100.0,
                )
            )
    return bars


class TestMarkovAndWalkForward(unittest.TestCase):
    def test_markov_blocks_long_in_bear_and_scales_short(self) -> None:
        cfg = MarkovRegimeConfig(
            enabled=True,
            return_lookback_days=20,
            bull_return_threshold_ratio=0.05,
            bear_return_threshold_ratio=0.05,
            transition_window_days=180,
            min_history_states=15,
            forecast_horizon_days=1,
            laplace_alpha=1.0,
            block_long_in_bear=True,
            block_short_in_bull=False,
            min_net_score_for_long_ratio=None,
            min_net_score_for_short_ratio=None,
            dynamic_sizing_enabled=True,
            high_persistence_threshold_ratio=0.70,
            aligned_high_persistence_multiplier=1.25,
            sideways_size_multiplier=0.75,
            counter_regime_size_multiplier=0.50,
        )
        flt = MarkovRegimeFilter(cfg)
        # 20 дней флэта (100.0), затем 25 дней устойчивого падения (100 -> 60)
        closes = [100.0] * 20 + [100.0 - i * 1.5 for i in range(1, 26)]
        for bar in _daily_4h_series(closes):
            flt.update("ADAUSDT", bar)

        dec_long = flt.evaluate("ADAUSDT", "LONG")
        dec_short = flt.evaluate("ADAUSDT", "SHORT")
        self.assertEqual(dec_long.current_state, "BEAR")
        self.assertFalse(dec_long.allowed)
        self.assertTrue(dec_short.allowed)
        self.assertAlmostEqual(dec_short.size_multiplier, 1.25)

    def test_markov_disabled_passthrough(self) -> None:
        cfg = MarkovRegimeConfig(
            enabled=False,
            return_lookback_days=20,
            bull_return_threshold_ratio=0.05,
            bear_return_threshold_ratio=0.05,
            transition_window_days=None,
            min_history_states=15,
            forecast_horizon_days=1,
            laplace_alpha=1.0,
            block_long_in_bear=True,
            block_short_in_bull=True,
            min_net_score_for_long_ratio=None,
            min_net_score_for_short_ratio=None,
            dynamic_sizing_enabled=True,
            high_persistence_threshold_ratio=0.70,
            aligned_high_persistence_multiplier=1.25,
            sideways_size_multiplier=0.75,
            counter_regime_size_multiplier=0.50,
        )
        flt = MarkovRegimeFilter(cfg)
        dec = flt.evaluate("ADAUSDT", "LONG")
        self.assertTrue(dec.allowed)
        self.assertAlmostEqual(dec.size_multiplier, 1.0)


if __name__ == "__main__":
    unittest.main()
