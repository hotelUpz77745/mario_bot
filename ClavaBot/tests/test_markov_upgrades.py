# Path: ClavaBot/tests/test_markov_upgrades.py
# Role: Юнит-тесты апгрейдов Маркова (GMM/Quantile пороги, wide SL, Chandelier, Breakeven, on_flip_only)

from __future__ import annotations

import dataclasses
import math
import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ClavaBot.CORE.config_loader import MarketSentinelConfig, MarkovRegimeConfig, StrategyPassport, StrictConfigLoader
from ClavaBot.CORE.models import Candle, OpenPosition, OrderPlan
from ClavaBot.CORE.orchestrator import TradeEngineOrchestrator
from ClavaBot.CORE.spec_manager import SpecManager
from ClavaBot.CORE.STRATEGY._indicators import (
    IncrementalADX,
    RollingEfficiencyRatio,
    calc_atr_volatility_multiplier,
    calc_wide_atr_sl_extreme,
)
from ClavaBot.CORE.STRATEGY._markov_thresholds import AdaptiveThresholdFinder
from ClavaBot.CORE.STRATEGY.market_sentinel import MarketSentinel
from ClavaBot.CORE.STRATEGY.signal_registry import SignalPluginRegistry
from ClavaBacktest.ENGINE.sim_broker import SimExecutionBroker


def _clone_markov_variant(
    base: StrategyPassport,
    params_override: dict | None = None,
    markov_override: dict | None = None,
    risk_override: dict | None = None,
) -> StrategyPassport:
    new_params = dict(base.signal.params)
    if params_override:
        new_params.update(params_override)
    new_sig = dataclasses.replace(base.signal, params=new_params)
    new_mk = dataclasses.replace(base.markov_regime, **markov_override) if markov_override else base.markov_regime
    new_risk = dataclasses.replace(base.risk_management, **risk_override) if risk_override else base.risk_management
    return dataclasses.replace(base, signal=new_sig, markov_regime=new_mk, risk_management=new_risk)


def _daily_series(prices: list[float], sym: str = "ADAUSDT") -> list[Candle]:
    bars: list[Candle] = []
    t0 = 1_704_067_200_000
    for d_idx, px in enumerate(prices):
        day_ms = t0 + d_idx * 86_400_000
        for sub in range(6):
            op = day_ms + sub * 14_400_000
            bars.append(Candle(sym, op, op + 14_400_000, px, px * 1.01, px * 0.99, px, 100.0))
    return bars


class TestMarkovUpgrades(unittest.TestCase):
    def setUp(self) -> None:
        self.app_cfg = StrictConfigLoader.read_json(ROOT_DIR / "ClavaBot" / "CFG" / "app.json")
        self.spec_mgr = SpecManager.from_app_config(ROOT_DIR / "ClavaBot", self.app_cfg)
        self.strat_dir = ROOT_DIR / "ClavaBot" / "CFG" / "artifacts"
        self.base_passport = StrictConfigLoader.load_strategy_passport(self.strat_dir / "markov_pure_4h_gmm_conf.json")

    def test_adaptive_gmm_and_quantile_thresholds(self) -> None:
        rets = [-0.12 + (i % 5) * 0.005 for i in range(40)] + [0.001 * (i % 6 - 3) for i in range(50)] + [0.10 + (i % 5) * 0.005 for i in range(40)]
        b_gmm, s_gmm = AdaptiveThresholdFinder.find_gmm_thresholds(rets)
        self.assertGreater(b_gmm, 0.02)
        self.assertGreater(s_gmm, 0.02)

        b_q, s_q = AdaptiveThresholdFinder.find_quantile_thresholds(rets)
        self.assertGreater(b_q, 0.01)
        self.assertGreater(s_q, 0.01)

    def test_wide_pct_sl_and_reentry_on_flip_only(self) -> None:
        p = _clone_markov_variant(
            self.base_passport,
            {"threshold_mode": "fixed", "sl_mode": "pct_from_entry", "sl_pct_ratio": 0.12, "reentry_mode": "on_flip_only", "confidence_sizing_enabled": False},
            risk_override={"sl_anchor": "origin_swing_extreme"},
        )
        broker = SimExecutionBroker(1000.0, 5, 0.0005, 0.0002, 0.0001, 0.0)
        orch = TradeEngineOrchestrator(p, self.spec_mgr, broker)
        # Построить устойчивый бычий дрейф (60 дней роста) -> сигнал LONG с SL на -12%
        prices = [100.0 * math.exp(0.008 * d) for d in range(60)]
        plans = []
        for bar in _daily_series(prices):
            pl = orch.on_candle_close("ADAUSDT", bar)
            if pl is not None:
                plans.append(pl)
                # Имитируем что позиция сразу закрылась (например по SL), чтобы проверить блокировку повторного входа!
                broker.positions.clear()
                broker.pending_orders.clear()

        self.assertEqual(len(plans), 1)  # reentry_mode="on_flip_only" -> вход ровно 1 раз без повторного входа на следующий день!
        self.assertIsNotNone(plans[0].stop_loss)
        assert plans[0].stop_loss is not None
        self.assertAlmostEqual(plans[0].stop_loss, plans[0].entry_ref_price * 0.88, places=3)

    def test_chandelier_and_breakeven_exits(self) -> None:
        # Chandelier exit
        p_chan = _clone_markov_variant(
            self.base_passport,
            {"threshold_mode": "fixed", "sl_mode": "chandelier_atr", "atr_sl_mult": 3.5, "chandelier_atr_mult": 3.0, "reentry_mode": "on_flip_only"},
            risk_override={"sl_anchor": "origin_swing_extreme"},
        )
        pl_chan = SignalPluginRegistry.create(p_chan)
        for bar in _daily_series([100.0] * 25):
            pl_chan.update_and_evaluate("ETHUSDT", bar)
        pos = OpenPosition("ETHUSDT", "LONG", 1.0, 100.0, 1_704_067_200_000, 90.0, None, 20.0, 0.1)
        # Рост до 130 (обновляет peak), затем падение ниже 130 - 3*ATR -> SIGNAL_EXIT
        self.assertIsNone(pl_chan.check_exit("ETHUSDT", Candle("ETHUSDT", 1, 2, 128.0, 130.0, 127.0, 129.0, 10.0), pos))
        self.assertEqual(
            pl_chan.check_exit("ETHUSDT", Candle("ETHUSDT", 2, 3, 120.0, 120.0, 110.0, 112.0, 10.0), pos),
            "SIGNAL_EXIT",
        )

        # Breakeven exit
        p_be = _clone_markov_variant(
            self.base_passport,
            {"threshold_mode": "fixed", "sl_mode": "breakeven_atr", "atr_sl_mult": 3.5, "be_trigger_atr_mult": 2.0, "reentry_mode": "on_flip_only"},
            risk_override={"sl_anchor": "origin_swing_extreme"},
        )
        pl_be = SignalPluginRegistry.create(p_be)
        for bar in _daily_series([100.0] * 25):
            pl_be.update_and_evaluate("ETHUSDT", bar)
        # ATR ~ 2.0, trigger 2*ATR=4 -> high=106 взводит БУ, затем close=100.05 (<= 100.12) закрывает в БУ
        self.assertIsNone(pl_be.check_exit("ETHUSDT", Candle("ETHUSDT", 1, 2, 102.0, 106.0, 101.5, 105.0, 10.0), pos))
        self.assertEqual(
            pl_be.check_exit("ETHUSDT", Candle("ETHUSDT", 2, 3, 101.0, 101.5, 100.0, 100.05, 10.0), pos),
            "SIGNAL_EXIT",
        )

    def test_gmm_confidence_sizing_and_k2_dual_smoke(self) -> None:
        variants = (
            _clone_markov_variant(self.base_passport, {"confidence_sizing_enabled": False}),
            _clone_markov_variant(self.base_passport, {"threshold_mode": "fixed", "confidence_sizing_enabled": True}),
            _clone_markov_variant(self.base_passport, {"threshold_mode": "fixed", "dual_confirm_gmm": True, "confidence_sizing_enabled": False}, {"forecast_horizon_days": 2}),
        )
        prices = [100.0 * (1.0 + 0.005 * math.sin(d / 5.0)) + 0.2 * d for d in range(70)]
        for p in variants:
            pl = SignalPluginRegistry.create(p)
            for bar in _daily_series(prices):
                pl.update_and_evaluate("ADAUSDT", bar)

    def test_gmm_conf_hybrid_and_catastrophic_sl(self) -> None:
        variants = (
            self.base_passport,
            _clone_markov_variant(self.base_passport, {"sl_mode": "wide_atr", "atr_sl_mult": 5.5, "reentry_mode": "on_flip_only"}),
            _clone_markov_variant(self.base_passport, {"sl_mode": "pct_from_entry", "sl_pct_ratio": 0.18, "reentry_mode": "on_flip_only"}),
        )
        prices = [100.0 * math.exp(0.008 * d) for d in range(70)]
        pos = OpenPosition("ADAUSDT", "LONG", 1.0, 100.0, 1_704_067_200_000, 80.0, None, 20.0, 0.1)

        for p in variants:
            pl = SignalPluginRegistry.create(p)
            self.assertEqual(p.signal.params["threshold_mode"], "gmm_3state")
            self.assertTrue(p.signal.params["confidence_sizing_enabled"])
            self.assertNotEqual(p.signal.params["sl_mode"], "chandelier_atr")
            self.assertIsNone(pl.check_exit("ADAUSDT", Candle("ADAUSDT", 1, 2, 100.0, 101.0, 99.0, 100.0, 10.0), pos))

            sigs = [s for bar in _daily_series(prices) if (s := pl.update_and_evaluate("ADAUSDT", bar)) is not None]
            self.assertGreater(len(sigs), 0)
            self.assertGreaterEqual(sigs[0].size_multiplier, 0.5)
            self.assertLessEqual(sigs[0].size_multiplier, 1.75)
            if p.signal.params["sl_mode"] == "pct_from_entry":
                assert sigs[0].origin_extreme_price is not None
                self.assertAlmostEqual(sigs[0].origin_extreme_price, sigs[0].entry_ref_price * 0.82, places=3)
            elif p.signal.params["sl_mode"] == "wide_atr":
                assert sigs[0].origin_extreme_price is not None
                self.assertLess(sigs[0].origin_extreme_price, sigs[0].entry_ref_price)

    def test_anti_chop_indicators_and_probe_scale_in(self) -> None:
        # 1. IncrementalADX & RollingEfficiencyRatio различают тренд и флэт
        adx_trend, adx_chop = IncrementalADX(14), IncrementalADX(14)
        er_trend, er_chop = RollingEfficiencyRatio(18), RollingEfficiencyRatio(18)
        for i in range(40):
            px_t = 100.0 + i * 1.5
            adx_trend.update(Candle("ADAUSDT", i, i + 1, px_t - 0.5, px_t + 0.8, px_t - 0.8, px_t, 10.0))
            er_trend.update(px_t)
            px_c = 100.0 + (1.5 if i % 2 == 0 else -1.5)
            adx_chop.update(Candle("ADAUSDT", i, i + 1, px_c, px_c + 1.0, px_c - 1.0, px_c, 10.0))
            er_chop.update(px_c)
        assert adx_trend.value is not None and adx_chop.value is not None
        assert er_trend.value is not None and er_chop.value is not None
        self.assertGreater(adx_trend.value, adx_chop.value)
        self.assertGreater(er_trend.value, 0.90)
        self.assertLess(er_chop.value, 0.10)

        # 2. Проверить загрузку и дым-прогон всех 3 новых паспортов анти-боковика
        for fname in (
            "markov_pure_4h_gmm_conf_antichop.json",
            "ema_cross_7_25_4h_markov_adx20.json",
            "ema_cross_7_25_4h_markov_probe20.json",
        ):
            p = StrictConfigLoader.load_strategy_passport(self.strat_dir / fname)
            pl = SignalPluginRegistry.create(p)
            self.assertIsNotNone(pl)

        # 3. Проверить 20% -> 100% scale-in через TradeEngineOrchestrator + SimExecutionBroker
        p_probe = StrictConfigLoader.load_strategy_passport(self.strat_dir / "ema_cross_7_25_4h_markov_probe20.json")
        broker = SimExecutionBroker(10000.0, 2, 0.0005, 0.0002, 0.0001, 0.0)
        _ = TradeEngineOrchestrator(p_probe, self.spec_mgr, broker)
        p0 = OrderPlan("ADAUSDT", "LONG", 100.0, 1.0, None, None, 1000, 100.0)
        self.assertTrue(broker.submit_market_with_bracket(p0))
        broker.fill_pending_at_minute_open("ADAUSDT", 2000, 1.0)
        self.assertEqual(broker.positions["ADAUSDT"].qty, 100.0)
        p_add = OrderPlan("ADAUSDT", "LONG", 400.0, 1.1, None, None, 3000, 440.0)
        self.assertTrue(broker.submit_scale_in_market(p_add))
        broker.fill_pending_at_minute_open("ADAUSDT", 4000, 1.1)
        self.assertAlmostEqual(broker.positions["ADAUSDT"].qty, 500.0, places=4)
        self.assertGreater(broker.positions["ADAUSDT"].entry_price, 1.0)

    def test_atr_vol_sizing_sentinel_and_final_passports(self) -> None:
        # 1. Обратно пропорциональный сайзинг по ATR% + кламп [0.35, 1.25] + 5*ATR SL
        self.assertAlmostEqual(calc_atr_volatility_multiplier(1.0, 100.0, 0.02, 0.35, 1.25), 1.25, places=6)
        self.assertAlmostEqual(calc_atr_volatility_multiplier(8.0, 100.0, 0.02, 0.35, 1.25), 0.35, places=6)
        self.assertAlmostEqual(calc_atr_volatility_multiplier(2.0, 100.0, 0.02, 0.35, 1.25), 1.0, places=6)
        self.assertAlmostEqual(calc_wide_atr_sl_extreme("LONG", 100.0, 2.0, 5.0) or 0.0, 90.0, places=6)
        self.assertAlmostEqual(calc_wide_atr_sl_extreme("SHORT", 100.0, 2.0, 5.0) or 0.0, 110.0, places=6)

        # 2. MarketSentinel: BTC Sentiment + Market Breadth (< 3 из 6 в тренде -> defensive mult)
        ms_cfg = MarketSentinelConfig(
            enabled=True,
            benchmark_symbol="BTCUSDT",
            btc_sentiment_enabled=True,
            btc_ema_period=20,
            btc_adx_period=14,
            btc_chop_adx_threshold=18.0,
            btc_ret_lookback_days=10,
            btc_chop_size_multiplier=0.5,
            btc_counter_long_multiplier=0.4,
            btc_counter_short_multiplier=0.5,
            block_long_in_btc_bear=True,
            breadth_enabled=True,
            breadth_adx_period=14,
            breadth_min_adx=18.0,
            breadth_min_trending_coins=3,
            breadth_defensive_multiplier=0.5,
        )
        syms = ["ADAUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ETHUSDT", "LTCUSDT"]
        sentinel = MarketSentinel(ms_cfg, syms)
        t0 = 1_704_067_200_000
        for d in range(30):
            px = 60_000.0 - d * 500.0
            for b in range(6):
                ts = t0 + (d * 6 + b) * 14_400_000
                sentinel.update_benchmark(Candle("BTCUSDT", ts, ts + 14_400_000, px + 50.0, px + 100.0, px - 100.0, px, 100.0))
        self.assertEqual(sentinel.btc_regime, "BEAR")
        dec_long = sentinel.evaluate("LONG")
        self.assertFalse(dec_long.allowed)

        for idx, s in enumerate(syms):
            sentinel.update_symbol(
                s,
                Candle(s, t0, t0 + 14_400_000, 100.0, 101.0, 99.0, 100.0, 10.0),
                "SIDEWAYS" if idx < 4 else "BULL",
            )
        dec_short = sentinel.evaluate("SHORT")
        self.assertTrue(dec_short.allowed)
        self.assertEqual(dec_short.trending_coins, 2)
        self.assertAlmostEqual(dec_short.size_multiplier, 0.5, places=6)

        # 3. Загрузка и проверка всех финальных продакшн-паспортов
        for fname in (
            "markov_pure_4h_gmm_conf_antichop_atr_btc_sl5.json",
            "markov_pure_4h_gmm_conf_antichop_exit_sw_sl5.json",
            "markov_pure_4h_gmm_conf_antichop_shield.json",
            "ema_cross_7_25_4h_markov_probe20_atr_sl5.json",
            "ema_cross_7_25_4h_markov_hybrid_sl5.json",
        ):
            p = StrictConfigLoader.load_strategy_passport(self.strat_dir / fname)
            pl = SignalPluginRegistry.create(p)
            self.assertIsNotNone(pl)
            self.assertEqual(p.risk_management.sl_anchor, "origin_swing_extreme")
            self.assertAlmostEqual(p.signal.params["target_atr_pct_ratio"], 0.02)
            self.assertAlmostEqual(p.signal.params["atr_sl_mult"], 5.0)
            self.assertIn("LTCUSDT", p.symbols)

        # 4. Проверка exit_on_sideways -> SIGNAL_EXIT при входе GMM в SIDEWAYS
        p_sw = StrictConfigLoader.load_strategy_passport(
            self.strat_dir / "markov_pure_4h_gmm_conf_antichop_exit_sw_sl5.json"
        )
        pl_sw = SignalPluginRegistry.create(p_sw)
        sw_ts = 1_704_067_200_000 + 25 * 86_400_000
        sw_day = (sw_ts + 14_400_000 - 1) // 86_400_000
        pl_sw._daily_state_snapshot["ADAUSDT"] = (sw_day, "SIDEWAYS", None)
        pos_open = OpenPosition("ADAUSDT", "LONG", 100.0, 1.0, sw_ts - 86_400_000, None, None, 50.0, 0.1)
        self.assertEqual(
            pl_sw.check_exit(
                "ADAUSDT",
                Candle("ADAUSDT", sw_ts, sw_ts + 14_400_000, 1.0, 1.02, 0.99, 1.01, 10.0),
                pos_open,
            ),
            "SIGNAL_EXIT",
        )


if __name__ == "__main__":
    unittest.main()
