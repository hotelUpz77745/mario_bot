# Path: MarioBot/tests/test_strategy_rudiments.py
# Role: Юнит-тесты классов-рудиментов стратегии и строгого загрузчика конфигов

from __future__ import annotations

import copy
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from MarioBot.CORE.config_loader import (
    ChochDeterminatorConfig,
    CapitalConfig,
    RiskManagementConfig,
    SessionWindowConfig,
    StrictConfigLoader,
    SwingDetectorConfig,
    TrendFatigueConfig,
)
from MarioBot.CORE.models import Candle, SignalEvent
from MarioBot.CORE.spec_manager import SpecManager
from MarioBot.CORE.STRATEGY.choch_determinator import ChochBreakoutDeterminator
from MarioBot.CORE.STRATEGY.position_sizer import PositionSizer
from MarioBot.CORE.STRATEGY.risk_sl_tp_calculator import RiskStopTargetCalculator
from MarioBot.CORE.STRATEGY.session_window_filter import SessionWindowFilter
from MarioBot.CORE.STRATEGY.swing_detector import SwingDetector
from MarioBot.CORE.STRATEGY.trend_fatigue_filter import TrendFatigueFilter


def _make_candle(idx: int, o: float, h: float, l: float, c: float, symbol: str = "ETHUSDT") -> Candle:
    open_ms = 1_700_000_000_000 + idx * 14_400_000
    return Candle(
        symbol=symbol,
        open_ts_ms=open_ms,
        close_ts_ms=open_ms + 14_400_000,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=100.0,
    )


class TestStrategyRudiments(unittest.TestCase):
    def setUp(self) -> None:
        app_cfg = StrictConfigLoader.read_json(ROOT_DIR / "MarioBot" / "CFG" / "app.json")
        self.spec_manager = SpecManager.from_app_config(ROOT_DIR / "MarioBot", app_cfg)

    def test_strict_config_loader_all_passports_and_missing_key(self) -> None:
        app_cfg = StrictConfigLoader.read_json(ROOT_DIR / "MarioBot" / "CFG" / "app.json")
        strat_dir = ROOT_DIR / "MarioBot" / "CFG" / "strategies"
        art_dir = ROOT_DIR / "MarioBot" / "CFG" / "artifacts"
        files = sorted(strat_dir.glob("*.json"))
        self.assertEqual(
            [f.name for f in files],
            [
                "markov_pure_4h_gmm_conf_antichop_exit_sw_sl5_top14_r15.json",
                "markov_pure_4h_gmm_conf_antichop_exit_sw_sl5_v1_stale_drift.json",
                "markov_pure_4h_gmm_conf_antichop_exit_sw_sl5_v2_profit_trail10.json",
                "markov_pure_4h_gmm_conf_antichop_exit_sw_sl5_v3_maxhold14d.json",
            ],
        )
        # Боевой паспорт строго привязан к слоту из app.json (никаких двойных источников истины!)
        primary_slot = app_cfg["active_strategies"]["markov_alpha_top14_main"]
        for p_file in files:
            raw_p = StrictConfigLoader.read_json(p_file)
            for forbidden in ("symbols", "enabled", "capital", "timeframe"):
                self.assertNotIn(forbidden, raw_p)
            passport = StrictConfigLoader.load_strategy_passport(p_file, slot_cfg=primary_slot, slot_id="markov_alpha_top14_main")
            self.assertEqual(passport.timeframe, "4h")
            # Попытка подсунуть symbols/capital в боевой паспорт обязана падать по Правилу 3.1
            bad_dup = copy.deepcopy(raw_p)
            bad_dup["symbols"] = ["BTCUSDT"]
            with self.assertRaises(ValueError):
                StrictConfigLoader.parse_strategy_passport(bad_dup, slot_cfg=primary_slot)

        for p_file in sorted(art_dir.glob("*.json")):
            passport = StrictConfigLoader.load_strategy_passport(p_file)
            self.assertTrue(len(passport.symbols) >= 1)
            self.assertIn(passport.timeframe, ("1h", "4h", "1d"))

        raw = StrictConfigLoader.read_json(files[0])
        broken = copy.deepcopy(raw)
        del broken["signal"]["plugin"]
        with self.assertRaises(KeyError):
            StrictConfigLoader.parse_strategy_passport(broken, slot_cfg=primary_slot)

    def test_swing_detector_no_lookahead_and_choch_trigger(self) -> None:
        detector = SwingDetector(
            "ETHUSDT",
            SwingDetectorConfig(left_candles=2, right_candles=2, min_swing_sequence=2),
        )
        # Построение медвежьей структуры: SH1(110), SL1(95), LH(105), LL(90)
        highs = [100, 104, 110, 103, 98, 96, 101, 105, 100, 95, 92, 93, 96]
        lows =  [ 98,  99, 102,  97, 95, 96,  98,  99,  94, 90, 91, 91, 93]
        closes= [ 99, 103, 108,  99, 96, 97, 100, 103,  96, 91, 92, 92, 95]

        last_state = None
        for i in range(len(highs)):
            c = _make_candle(i, closes[i] - 0.5, highs[i], lows[i], closes[i])
            last_state = detector.update(c)

        assert last_state is not None
        self.assertEqual(last_state.regime, "BEARISH")
        self.assertIsNotNone(last_state.last_lh)
        self.assertAlmostEqual(last_state.last_lh.price, 105.0)

        # Пробой тела выше LH=105 -> LONG CHoCH ровно один раз
        choch = ChochBreakoutDeterminator(
            ChochDeterminatorConfig(breakout_price_source="close", require_sweep=False, entry_order_type="MARKET")
        )
        breakout_candle = _make_candle(20, 102.0, 107.0, 101.0, 106.5)
        st_break = detector.update(breakout_candle)
        sig1 = choch.evaluate(breakout_candle, st_break)
        self.assertIsNotNone(sig1)
        assert sig1 is not None
        self.assertEqual(sig1.side, "LONG")
        self.assertAlmostEqual(sig1.broken_level_price, 105.0)

        # Следующая свеча не должна повторно открывать CHoCH на том же уровне
        next_candle = _make_candle(21, 106.5, 108.0, 105.5, 107.0)
        st_next = detector.update(next_candle)
        self.assertIsNone(choch.evaluate(next_candle, st_next))

    def test_session_window_ny(self) -> None:
        flt = SessionWindowFilter(
            SessionWindowConfig(
                enabled=True,
                timezone="America/New_York",
                start_time="08:00",
                end_time="16:00",
                weekdays_only=True,
            )
        )
        # Четверг 2026-10-15 16:00 UTC = 12:00 EDT (разрешено)
        ok_ms = int(datetime(2026, 10, 15, 16, 0, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertTrue(flt.is_allowed(ok_ms))

        # Четверг 2026-10-15 08:00 UTC = 04:00 EDT (запрещено)
        early_ms = int(datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertFalse(flt.is_allowed(early_ms))

        # Суббота 2026-10-17 16:00 UTC (выходной при weekdays_only=True)
        sat_ms = int(datetime(2026, 10, 17, 16, 0, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertFalse(flt.is_allowed(sat_ms))

    def test_trend_fatigue_filter(self) -> None:
        flt = TrendFatigueFilter(
            TrendFatigueConfig(
                enabled=True,
                lookback_days=3,
                min_net_efficiency_ratio=0.55,
                max_counter_pullback_ratio=0.20,
                block_mode="same_direction_as_fatigue",
            ),
            timeframe="4h",
        )
        # 18 монотонно растущих 4H свечей
        for i in range(18):
            base = 100.0 + i * 2.0
            flt.update("ETHUSDT", _make_candle(i, base, base + 2.2, base - 0.1, base + 2.0))

        self.assertTrue(flt.is_fatigued("ETHUSDT", "LONG"))
        self.assertFalse(flt.is_fatigued("ETHUSDT", "SHORT"))

    def test_risk_calculator_and_position_sizer(self) -> None:
        calc_no_buf = RiskStopTargetCalculator(
            RiskManagementConfig(sl_anchor="origin_swing_extreme", sl_buffer_ratio=None, tp_rr_ratio=1.0, soft_stop_enabled=False),
            self.spec_manager,
        )
        calc_buf = RiskStopTargetCalculator(
            RiskManagementConfig(sl_anchor="origin_swing_extreme", sl_buffer_ratio=0.01, tp_rr_ratio=1.0, soft_stop_enabled=False),
            self.spec_manager,
        )
        sig = SignalEvent("ETHUSDT", "LONG", 1_700_000_000_000, 2000.0, 1980.0, 5, 1900.0)
        ok0, sl0, tp0 = calc_no_buf.calculate(sig)
        self.assertTrue(ok0)
        assert sl0 is not None and tp0 is not None
        self.assertAlmostEqual(sl0, 1900.0)
        self.assertAlmostEqual(tp0, 2100.0)

        ok1, sl1, tp1 = calc_buf.calculate(sig)
        self.assertTrue(ok1)
        assert sl1 is not None and tp1 is not None
        self.assertAlmostEqual(sl1, 1881.0)
        self.assertAlmostEqual(tp1, 2119.0)

        sizer = PositionSizer(
            CapitalConfig(start_balance_usd=1000.0, leverage=5, max_open_positions=6, risk_per_trade_ratio=0.01, fixed_notional_usd=None),
            self.spec_manager,
        )
        qty = sizer.calculate_qty("ETHUSDT", equity_usd=1000.0, free_margin_usd=1000.0, entry_price=2000.0, stop_loss_price=1900.0)
        # Риск $10 / $100 = 0.1 ETH (нотионал $200 <= $5000 марж. лимита)
        self.assertAlmostEqual(qty, 0.1)


if __name__ == "__main__":
    unittest.main()
