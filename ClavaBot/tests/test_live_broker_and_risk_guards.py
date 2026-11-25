# Path: ClavaBot/tests/test_live_broker_and_risk_guards.py
# Role: Юнит-тесты BinanceFuturesExecutionBroker, LiveRunner, PortfolioRiskGuard, PoolSelector и Funding

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ClavaBacktest.ENGINE.sim_broker import SimExecutionBroker
from ClavaBot.CORE.config_loader import PortfolioRiskConfig, StrictConfigLoader
from ClavaBot.CORE.EXECUTION.binance_futures_broker import (
    BinanceFuturesConfig,
    BinanceFuturesExecutionBroker,
    BinanceFuturesRestClient,
)
from ClavaBot.CORE.EXECUTION.live_runner import BinanceFuturesLiveRunner
from ClavaBot.CORE.models import OpenPosition, OrderPlan
from ClavaBot.CORE.orchestrator import TradeEngineOrchestrator
from ClavaBot.CORE.spec_manager import SpecManager
from ClavaBot.CORE.STRATEGY.pool_selector import PoolSelectorConfig, WalkForwardPoolSelector
from ClavaBot.CORE.STRATEGY.portfolio_risk_guard import PortfolioRiskGuard
from ClavaBot.main import build_live_runtime


class _FakeBinanceTransport:
    """Детерминированный мок HTTP-транспорта Binance Futures для оффлайн-тестов."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        params: Optional[Dict[str, Any]],
        timeout_sec: float,
    ) -> Any:
        path = url.split("fapi.binance.com")[-1]
        p = dict(params or {})
        self.calls.append((method, path, p))

        if path == "/fapi/v1/time":
            return {"serverTime": 1_700_000_000_000}
        if path == "/fapi/v1/exchangeInfo":
            return {
                "symbols": [
                    {
                        "symbol": "SOLUSDT",
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                            {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
                            {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
                        ],
                    },
                    {
                        "symbol": "BTCUSDT",
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                            {"filterType": "MIN_NOTIONAL", "notional": "50.0"},
                        ],
                    },
                ]
            }
        if path == "/fapi/v2/account":
            return {"totalMarginBalance": "12500.50", "availableBalance": "9800.25"}
        if path == "/fapi/v2/positionRisk":
            return [
                {
                    "symbol": "SOLUSDT",
                    "positionAmt": "10.5",
                    "entryPrice": "180.0",
                    "leverage": "2",
                },
                {
                    "symbol": "ADAUSDT",
                    "positionAmt": "0",
                    "entryPrice": "0.0",
                    "leverage": "2",
                },
            ]
        if path == "/fapi/v1/openOrders":
            return [{"symbol": "SOLUSDT", "type": "STOP_MARKET", "stopPrice": "155.0", "orderId": 777}]
        if path == "/fapi/v1/klines":
            sym = p["symbol"]
            base_px = 60000.0 if sym == "BTCUSDT" else 150.0
            start = int(p["startTime"])
            rows = []
            for i in range(5):
                o_ms = start + i * 14_400_000
                rows.append([o_ms, base_px, base_px * 1.01, base_px * 0.99, base_px * 1.005, 1000.0, o_ms + 14_399_999])
            return rows
        if path == "/fapi/v1/premiumIndex":
            return {"symbol": p["symbol"], "lastFundingRate": "0.0006"}
        if path == "/fapi/v1/order":
            if p.get("type") == "MARKET":
                return {"orderId": 1001, "executedQty": p["quantity"], "avgPrice": "200.0"}
            return {"orderId": 1002}
        if path == "/fapi/v1/allOpenOrders":
            return {"code": 200, "msg": "ok"}
        if path in ("/fapi/v1/leverage", "/fapi/v1/marginType"):
            return {"ok": True}
        raise AssertionError(f"Unexpected endpoint {method} {path}")


class TestLiveBrokerAndRiskGuards(unittest.TestCase):
    def test_binance_live_broker_orders_reconcile_and_specs(self) -> None:
        app_cfg = StrictConfigLoader.read_json(ROOT_DIR / "ClavaBot" / "CFG" / "app.json")
        bf_cfg = dataclasses.replace(
            BinanceFuturesConfig.from_dict(app_cfg["binance_futures"]),
            dry_run=False,
        )
        transport = _FakeBinanceTransport()
        client = BinanceFuturesRestClient(bf_cfg, api_key="k", api_secret="s", transport=transport)
        spec_mgr = SpecManager(app_cfg["symbol_specs"])
        broker = BinanceFuturesExecutionBroker(bf_cfg, spec_mgr, client=client)

        self.assertEqual(broker.sync_symbol_specs(["SOLUSDT", "BTCUSDT"]), 2)
        self.assertEqual(broker.reconcile_positions(["SOLUSDT", "ADAUSDT"]), 1)
        pos = broker.get_open_position("SOLUSDT")
        self.assertIsNotNone(pos)
        assert pos is not None
        self.assertEqual(pos.side, "LONG")
        self.assertAlmostEqual(pos.stop_loss or 0.0, 155.0)
        self.assertAlmostEqual(broker.get_equity_usd(), 12500.50)

        # Закрытие: сначала DELETE /fapi/v1/allOpenOrders, затем MARKET reduceOnly
        transport.calls.clear()
        self.assertTrue(broker.submit_close_market("SOLUSDT", reason="SIGNAL_EXIT"))
        self.assertFalse(broker.has_open_position("SOLUSDT"))
        self.assertEqual(transport.calls[0][0], "DELETE")
        self.assertEqual(transport.calls[0][1], "/fapi/v1/allOpenOrders")
        self.assertEqual(transport.calls[1][0], "POST")
        self.assertEqual(transport.calls[1][1], "/fapi/v1/order")
        self.assertEqual(transport.calls[1][2]["reduceOnly"], "true")

        # Открытие с аварийным STOP_MARKET (MARK_PRICE, closePosition=true)
        transport.calls.clear()
        plan = OrderPlan("SOLUSDT", "LONG", 5.0, 200.0, 175.0, None, 1_700_000_000_000, 1000.0)
        self.assertTrue(broker.submit_market_with_bracket(plan))
        self.assertEqual(transport.calls[0][2]["type"], "MARKET")
        self.assertEqual(transport.calls[1][2]["type"], "STOP_MARKET")
        self.assertEqual(transport.calls[1][2]["workingType"], "MARK_PRICE")
        self.assertEqual(transport.calls[1][2]["closePosition"], "true")

    def test_live_runner_warmup_and_ws_dedup(self) -> None:
        runner = build_live_runtime("markov_pure_4h_gmm_conf_antichop_exit_sw_sl5_top15_r15.json")
        # Заменяем транспорт на фейковый с 2 символами для быстрого теста
        transport = _FakeBinanceTransport()
        runner.broker.client = BinanceFuturesRestClient(runner.cfg, api_key="k", api_secret="s", transport=transport)
        runner.orchestrator.passport = dataclasses.replace(runner.orchestrator.passport, symbols=["SOLUSDT"])
        replayed = runner.warmup_and_reconcile(now_ms=1_700_000_000_000)
        self.assertGreater(replayed, 0)
        self.assertTrue(runner.orchestrator.trading_enabled)

        # Незакрытая WS-свеча (x=False) игнорируется
        unclosed = {"data": {"e": "kline", "k": {"s": "SOLUSDT", "t": 1_700_000_000_000, "T": 1_700_014_399_999, "o": "150", "h": "155", "l": "149", "c": "152", "v": "10", "x": False}}}
        self.assertFalse(runner.process_ws_kline_message(unclosed))
        # Закрытая свеча обрабатывается ровно один раз (повторный close_ts отбрасывается)
        closed = {"data": {"e": "kline", "k": {"s": "SOLUSDT", "t": 1_700_000_000_000, "T": 1_700_014_399_999, "o": "150", "h": "155", "l": "149", "c": "152", "v": "10", "x": True}}}
        self.assertTrue(runner.process_ws_kline_message(closed))
        self.assertFalse(runner.process_ws_kline_message(closed))

    def test_portfolio_risk_guard_caps_dd_throttle_and_funding(self) -> None:
        cfg = PortfolioRiskConfig(
            enabled=True,
            max_same_side_positions=2,
            max_portfolio_risk_ratio=0.05,
            dd_throttle_enabled=True,
            dd_tier1_ratio=0.20,
            dd_tier1_multiplier=0.60,
            dd_tier2_ratio=0.35,
            dd_tier2_multiplier=0.35,
            funding_guard_enabled=True,
            max_adverse_funding_rate=0.0005,
            adverse_funding_multiplier=0.50,
        )
        guard = PortfolioRiskGuard(cfg, start_equity_usd=10000.0)
        p1 = OpenPosition("ADAUSDT", "LONG", 1000.0, 1.0, 0, 0.9, None, 500.0, 0.0)   # risk $100 (1%)
        p2 = OpenPosition("SOLUSDT", "LONG", 10.0, 100.0, 0, 90.0, None, 500.0, 0.0)   # risk $100 (1%)

        # 2 LONG уже открыты -> 3-й LONG заблокирован, но SHORT разрешён
        d_long = guard.evaluate("ETHUSDT", "LONG", 10000.0, {"ADAUSDT": p1, "SOLUSDT": p2})
        self.assertFalse(d_long.allowed)
        self.assertEqual(d_long.block_reason, "MAX_SAME_SIDE_POSITIONS")
        d_short = guard.evaluate("ETHUSDT", "SHORT", 10000.0, {"ADAUSDT": p1, "SOLUSDT": p2})
        self.assertTrue(d_short.allowed)

        # Open-risk cap (6% > 5%) -> блокировка
        p_big = OpenPosition("ETHUSDT", "LONG", 10.0, 1000.0, 0, 940.0, None, 5000.0, 0.0)  # risk $600 (6%)
        d_cap = guard.evaluate("AVAXUSDT", "SHORT", 10000.0, {"ETHUSDT": p_big})
        self.assertFalse(d_cap.allowed)
        self.assertEqual(d_cap.block_reason, "MAX_PORTFOLIO_RISK")

        # DD Throttle: HWM=10000 -> eq=7500 (DD=25% -> 0.60x); eq=6000 (DD=40% -> 0.35x) + adverse funding (0.50x)
        guard.update_funding_rate("AVAXUSDT", 0.0008)  # перегретый позитивный фандинг -> бьёт LONG
        d_t1 = guard.evaluate("AVAXUSDT", "SHORT", 7500.0, {})
        self.assertAlmostEqual(d_t1.size_multiplier, 0.60)
        d_t2_long = guard.evaluate("AVAXUSDT", "LONG", 6000.0, {})
        self.assertAlmostEqual(d_t2_long.size_multiplier, 0.35 * 0.50)

    def test_walk_forward_pool_selector_causal(self) -> None:
        cfg = PoolSelectorConfig(
            rebalance_interval_days=90,
            min_history_days=60,
            lookback_days=60,
            target_pool_size=2,
            min_median_atr_pct_ratio=0.02,
            min_avg_daily_quote_vol_usd=1_000_000.0,
        )
        sel = WalkForwardPoolSelector(cfg, ["TREND1", "TREND2", "FLAT_TRX", "YOUNG_HYPE"])
        t0 = 1_700_000_000_000
        for day in range(70):
            ts = t0 + day * 86_400_000 + 86_400_000
            px1 = 100.0 + day * 2.0
            sel.update_bar("TREND1", ts, px1 * 1.03, px1 * 0.98, px1, 500_000.0)
            px2 = 50.0 + day * 0.8
            sel.update_bar("TREND2", ts, px2 * 1.025, px2 * 0.985, px2, 500_000.0)
            # Плоская монета (range 0.5% < 2% порога)
            sel.update_bar("FLAT_TRX", ts, 0.1003, 0.0998, 0.10, 50_000_000.0)
            # Молодая монета (< 60 дней истории)
            if day >= 40:
                sel.update_bar("YOUNG_HYPE", ts, 20.6, 19.4, 20.0 + (day - 40) * 0.5, 500_000.0)

        changed, pool, _scores = sel.maybe_rebalance(t0 + 70 * 86_400_000)
        self.assertTrue(changed)
        self.assertEqual(pool, ["TREND1", "TREND2"])

    def test_sim_broker_apply_funding_cashflow(self) -> None:
        sim = SimExecutionBroker(10000.0, 2, 0.0005, 0.0002, 0.0001, 0.0)
        sim.positions["SOLUSDT"] = OpenPosition("SOLUSDT", "LONG", 10.0, 200.0, 1000, 170.0, None, 1000.0, 1.0)
        sim.positions["ADAUSDT"] = OpenPosition("ADAUSDT", "SHORT", 1000.0, 1.0, 1000, 1.2, None, 500.0, 0.5)
        cf_long = sim.apply_funding("SOLUSDT", 2000, 0.001, 200.0)   # -$2.00
        cf_short = sim.apply_funding("ADAUSDT", 2000, 0.001, 1.0)    # +$1.00
        self.assertAlmostEqual(cf_long, -2.0)
        self.assertAlmostEqual(cf_short, 1.0)
        self.assertAlmostEqual(sim.total_funding_usd, -1.0)
        self.assertAlmostEqual(sim.balance_usd, 9999.0)


if __name__ == "__main__":
    unittest.main()
