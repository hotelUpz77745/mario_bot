# Path: MarioBot/tests/test_time_grid_precompiler.py
# Role: Тесты единой системы прекомпиляции OHLCV, Funding Rates и индикаторов на общую ось времени

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
import numpy as np

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from MarioBacktest.ENGINE.time_grid_precompiler import (
    DEFAULT_MARKOV_INDICATOR_SPECS,
    PrecompiledTimeGrid,
    UnifiedTimeGridPrecompiler,
)
from MarioBot.CORE.models import Candle
from MarioBot.CORE.STRATEGY._indicators import IncrementalATR, RollingEfficiencyRatio


class TestTimeGridPrecompiler(unittest.TestCase):
    def test_unified_precompile_ohlcv_funding_and_indicators_exact_alignment(self) -> None:
        tf_ms = 14_400_000  # 4h
        n_bars = 25
        n_1m = n_bars * 240
        t0 = 1_700_006_400_000  # кратно 4h (00:00 UTC aligned)
        t0 = (t0 // tf_ms) * tf_ms
        ts_1m = np.arange(t0, t0 + n_1m * 60_000, 60_000, dtype=np.int64)
        ohlcv_1m = np.zeros((n_1m, 2, 5), dtype=np.float64)
        valid_1m = np.ones((n_1m, 2), dtype=bool)

        for i in range(n_1m):
            px0 = 100.0 + i * 0.01
            ohlcv_1m[i, 0] = [px0, px0 + 0.5, px0 - 0.3, px0 + 0.005, 10.0]
            px1 = 50.0 + (i % 60) * 0.05
            ohlcv_1m[i, 1] = [px1, px1 + 0.2, px1 - 0.2, px1 + 0.01, 20.0]

        # 8h funding события на барах 0, 2, 4 (ровно на границах 8h + 1ms как на Binance)
        raw_funding = {
            "SOLUSDT": (
                np.asarray([t0 + 1, t0 + 2 * tf_ms + 1, t0 + 4 * tf_ms], dtype=np.int64),
                np.asarray([0.0001, 0.0005, -0.0002], dtype=np.float64),
            ),
            "ETHUSDT": (
                np.asarray([t0 + 2 * tf_ms], dtype=np.int64),
                np.asarray([0.0003], dtype=np.float64),
            ),
        }

        grid = UnifiedTimeGridPrecompiler.compile_tf_grid(
            ts_1m_ms=ts_1m,
            ohlcv_1m=ohlcv_1m,
            valid_1m=valid_1m,
            symbols=["SOLUSDT", "ETHUSDT"],
            tf_ms=tf_ms,
            raw_funding=raw_funding,
            indicator_specs=DEFAULT_MARKOV_INDICATOR_SPECS,
        )

        self.assertEqual(grid.bar_open_ts_ms.shape[0], n_bars)
        # Проверка точной привязки Funding к индексам 0, 2, 4
        self.assertAlmostEqual(grid.get_funding(0, 0) or 0.0, 0.0001)
        self.assertIsNone(grid.get_funding(1, 0))
        self.assertAlmostEqual(grid.get_funding(2, 0) or 0.0, 0.0005)
        self.assertAlmostEqual(grid.get_funding(2, 1) or 0.0, 0.0003)
        self.assertAlmostEqual(grid.get_funding(4, 0) or 0.0, -0.0002)
        self.assertTrue(bool(grid.funding_any_bar[2]))
        self.assertFalse(bool(grid.funding_any_bar[3]))

        # Проверка битовой идентичности прекомпилированных ATR(14) и ER(18) с боевыми инкрементальными классами
        atr_live = IncrementalATR(14)
        er_live = RollingEfficiencyRatio(18)
        for b in range(n_bars):
            row = grid.ohlcv[b, 0]
            c = Candle("SOLUSDT", int(grid.bar_open_ts_ms[b]), int(grid.bar_close_ts_ms[b]), float(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]))
            a_val = atr_live.update(c)
            e_val = er_live.update(c.close)
            pre_a = grid.get_indicator("atr_14", b, 0)
            pre_e = grid.get_indicator("er_18", b, 0)
            if a_val is None:
                self.assertIsNone(pre_a)
            else:
                self.assertAlmostEqual(pre_a or 0.0, a_val, places=12)
            if e_val is None:
                self.assertIsNone(pre_e)
            else:
                self.assertAlmostEqual(pre_e or 0.0, e_val, places=12)

        # Проверка прекомпиляции 1m-сетки фандинга и сериализации save/load .npz
        f1m, m1m = UnifiedTimeGridPrecompiler.compile_funding_to_1m_grid(ts_1m, ["SOLUSDT", "ETHUSDT"], raw_funding)
        self.assertTrue(bool(m1m[0]))
        self.assertAlmostEqual(float(f1m[0, 0]), 0.0001)
        self.assertAlmostEqual(float(f1m[480, 0]), 0.0005)
        self.assertAlmostEqual(float(f1m[480, 1]), 0.0003)

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "grid.npz"
            grid.save_npz(p)
            loaded = PrecompiledTimeGrid.load_npz(p)
            self.assertEqual(loaded.symbols, grid.symbols)
            np.testing.assert_allclose(loaded.indicators["atr_14"], grid.indicators["atr_14"], equal_nan=True)
            np.testing.assert_allclose(loaded.funding_rates, grid.funding_rates)


if __name__ == "__main__":
    unittest.main()
