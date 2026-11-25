# Path: ClavaBot/tests/test_all_notes_plugins.py
# Role: Юнит-тесты выживших плагинов из notes.md (Gerchik, ICT, MACD Anti, Markov Pure)

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ClavaBot.CORE.config_loader import StrictConfigLoader
from ClavaBot.CORE.models import Candle
from ClavaBot.CORE.STRATEGY.signal_registry import SignalPluginRegistry


def _c(idx: int, o: float, h: float, l: float, c: float, ts_base: int = 1_769_904_000_000) -> Candle:
    t0 = ts_base + idx * 14_400_000
    return Candle("ETHUSDT", t0, t0 + 14_400_000, o, h, l, c, 100.0)


class TestAllNotesPlugins(unittest.TestCase):
    def setUp(self) -> None:
        self.strat_dir = ROOT_DIR / "ClavaBot" / "CFG" / "strategies"
        self.art_dir = ROOT_DIR / "ClavaBot" / "CFG" / "artifacts"

    def test_gerchik_two_bar_fp_triggers_short(self) -> None:
        p = StrictConfigLoader.load_strategy_passport(self.art_dir / "gerchik_fp_4h_markov.json")
        pl = SignalPluginRegistry.create(p)
        for i in range(22):
            pl.update_and_evaluate("ETHUSDT", _c(i, 100.0, 102.0, 98.0, 100.0))
        self.assertIsNone(pl.update_and_evaluate("ETHUSDT", _c(22, 100.5, 103.5, 100.0, 103.0)))
        sig = pl.update_and_evaluate("ETHUSDT", _c(23, 103.0, 103.2, 100.5, 101.0))
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.side, "SHORT")

    def test_ict_silver_bullet_fvg_after_sweep(self) -> None:
        p = StrictConfigLoader.load_strategy_passport(self.art_dir / "ict_silver_bullet_4h.json")
        pl = SignalPluginRegistry.create(p)
        for i in range(12):
            pl.update_and_evaluate("ETHUSDT", _c(i, 100.0, 102.0, 99.0, 100.5))
        pl.update_and_evaluate("ETHUSDT", _c(12, 100.0, 101.0, 97.0, 100.5))
        pl.update_and_evaluate("ETHUSDT", _c(13, 100.5, 103.5, 100.2, 103.0))
        sig = pl.update_and_evaluate("ETHUSDT", _c(14, 103.0, 106.0, 102.0, 105.5))
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.side, "LONG")

    def test_macd_anti_and_markov_pure_registry_smoke(self) -> None:
        for path in (self.art_dir / "macd_anti_4h.json", self.strat_dir / "markov_pure_4h_gmm_conf.json"):
            p = StrictConfigLoader.load_strategy_passport(path)
            pl = SignalPluginRegistry.create(p)
            for i in range(210):
                px = 100.0 + (i % 7) - 3.0
                pl.update_and_evaluate("ETHUSDT", _c(i, px, px + 1.5, px - 1.5, px + 0.3))


if __name__ == "__main__":
    unittest.main()
