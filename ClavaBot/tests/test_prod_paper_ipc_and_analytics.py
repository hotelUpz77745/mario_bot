# Path: ClavaBot/tests/test_prod_paper_ipc_and_analytics.py
# Role: Полные юнит-тесты боевого HEDGE-брокера, Master/Slave IPC failover, FeatureTimeline, ClavaPaper и LiveAnalytics

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import numpy as np

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ClavaBacktest.ENGINE.sim_broker import SimExecutionBroker
from ClavaBacktest.ENGINE.time_grid_precompiler import (
    DEFAULT_MARKOV_INDICATOR_SPECS,
    UnifiedTimeGridPrecompiler,
)
from ClavaBot.ANALYTICS.live_analytics import LiveAnalyticsManager
from ClavaBot.CORE.config_loader import PortfolioRiskConfig, StrictConfigLoader
from ClavaBot.CORE.DATA.feature_timeline import LiveRingFeatureTimeline, PrecompiledFeatureTimeline
from ClavaBot.CORE.EXECUTION.cluster_runtime import ClavaClusterRuntime
from ClavaBot.CORE.EXECUTION.hron_binance_broker import (
    HronBinanceConfig,
    HronBinanceProdBroker,
    HronBinanceRestAdapter,
)
from ClavaBot.CORE.EXECUTION.ipc_price_hub import (
    ClavaCrossProcessPriceHub,
    PriceTick,
    discover_cluster_symbols_from_configs,
)
from ClavaBot.CORE.models import Candle, ClosedTrade, OpenPosition, OrderPlan
from ClavaBot.CORE.spec_manager import SpecManager
from ClavaBot.CORE.STRATEGY.portfolio_risk_guard import PortfolioRiskGuard
from ClavaBot.main import build_live_cluster
from ClavaPaper.CORE.paper_broker import PaperExecutionBroker
from ClavaPaper.main import build_paper_cluster


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _FakeHedgeTransport:
    """Мок REST транспорта Binance Futures (HEDGE Mode)."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []

    def __call__(
        self, method: str, url: str, headers: Dict[str, str], params: Optional[Dict[str, Any]], timeout_sec: float
    ) -> Any:
        path = url.split("fapi.binance.com")[-1]
        p = dict(params or {})
        self.calls.append((method, path, p))
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
                    }
                ]
            }
        if path == "/fapi/v2/account":
            return {
                "availableBalance": "5000.00",
                "positions": [
                    {"symbol": "SOLUSDT", "positionSide": "LONG", "positionAmt": "2.5", "entryPrice": "180.0"},
                    {"symbol": "ADAUSDT", "positionSide": "LONG", "positionAmt": "0", "entryPrice": "0"},
                ],
            }
        if path == "/fapi/v1/openOrders":
            return [
                {"symbol": "SOLUSDT", "positionSide": "LONG", "type": "STOP_MARKET", "stopPrice": "155.0", "orderId": 501},
                {"symbol": "SOLUSDT", "positionSide": "SHORT", "type": "STOP_MARKET", "stopPrice": "210.0", "orderId": 502},
            ]
        if path == "/fapi/v1/batchOrders":
            return [{"orderId": 501, "status": "CANCELED"}]
        if path == "/fapi/v1/order":
            if p.get("type") == "MARKET":
                return {"orderId": 901, "executedQty": p["quantity"], "avgPrice": "200.0"}
            return {"orderId": 902}
        if path == "/fapi/v1/klines":
            start = int(p["startTime"])
            base = 60000.0 if p["symbol"] == "BTCUSDT" else 150.0
            return [[start + i * 14_400_000, base, base * 1.01, base * 0.99, base * 1.002, 500.0, start + (i + 1) * 14_400_000 - 1] for i in range(4)]
        if path == "/fapi/v1/premiumIndex":
            return {"symbol": p["symbol"], "lastFundingRate": "0.0004"}
        raise AssertionError(f"Unexpected REST call: {method} {path}")


class TestProdPaperIpcAndAnalytics(unittest.TestCase):
    def test_specs_cache_daily_refresh_without_app_hardcode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cpath = Path(tmp) / "specifications.json"
            mgr = SpecManager(cache_path=cpath, ttl_sec=86400.0)
            self.assertTrue(mgr.is_cache_stale())
            cnt = mgr.refresh_from_exchange(
                fetcher=lambda: {
                    "symbols": [
                        {
                            "symbol": "NEWCOINUSDT",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                                {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1"},
                                {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
                            ],
                        }
                    ]
                }
            )
            self.assertEqual(cnt, 1)
            self.assertFalse(mgr.is_cache_stale())
            reloaded = SpecManager.from_cache_file(cpath, ttl_sec=86400.0, auto_refresh=False)
            self.assertEqual(float(reloaded.get_spec("NEWCOINUSDT").tick_size), 0.001)

    def test_polymorphic_feature_timeline_parity_backtest_vs_live(self) -> None:
        tf_ms = 14_400_000
        n_bars = 22
        t0 = (1_700_006_400_000 // tf_ms) * tf_ms
        ts_1m = np.arange(t0, t0 + n_bars * 240 * 60_000, 60_000, dtype=np.int64)
        ohlcv = np.zeros((len(ts_1m), 1, 5), dtype=np.float64)
        valid = np.ones((len(ts_1m), 1), dtype=bool)
        for i in range(len(ts_1m)):
            p = 100.0 + i * 0.005
            ohlcv[i, 0] = [p, p + 0.3, p - 0.2, p + 0.002, 5.0]
        raw_fund = {"SOLUSDT": (np.asarray([t0 + 2 * tf_ms], dtype=np.int64), np.asarray([0.0005], dtype=np.float64))}

        grid = UnifiedTimeGridPrecompiler.compile_tf_grid(
            ts_1m, ohlcv, valid, ["SOLUSDT"], tf_ms, raw_funding=raw_fund, indicator_specs=DEFAULT_MARKOV_INDICATOR_SPECS
        )
        bt_tl = PrecompiledFeatureTimeline(grid)
        live_tl = LiveRingFeatureTimeline(["SOLUSDT"])

        for b in range(n_bars):
            bt_tl.advance_to_bar(b)
            s_bt = bt_tl.get_latest_snapshot("SOLUSDT")
            assert s_bt is not None
            fr_b = grid.get_funding(b, 0)
            s_live = live_tl.push_candle(s_bt.candle, funding_rate=fr_b)
            self.assertEqual(s_bt.close_ts_ms, s_live.close_ts_ms)
            self.assertEqual(s_bt.funding_rate, s_live.funding_rate)
            for ind_name in ("atr_14", "er_18"):
                v_bt = s_bt.indicators[ind_name]
                v_lv = s_live.indicators[ind_name]
                if v_bt is None:
                    self.assertIsNone(v_lv)
                else:
                    self.assertAlmostEqual(v_bt, v_lv or 0.0, places=12)

    def test_hron_hedge_broker_no_leverage_calls_and_side_isolated_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_cfg = StrictConfigLoader.read_json(ROOT_DIR / "ClavaBot" / "CFG" / "app.json")
            bf_cfg = HronBinanceConfig.from_dict({**app_cfg["binance_futures"], "dry_run": False})
            transport = _FakeHedgeTransport()
            rest = HronBinanceRestAdapter(bf_cfg, "API_KEY", "API_SECRET", api_key="k", api_secret="s", transport=transport)
            spec_mgr = SpecManager.from_app_config(ROOT_DIR / "ClavaBot", app_cfg)
            analytics = LiveAnalyticsManager("test_prod", "PROD", 1000.0, Path(tmp), ["SOLUSDT"])
            broker = HronBinanceProdBroker(bf_cfg, spec_mgr, rest, start_balance_usd=1000.0, analytics=analytics)

            self.assertEqual(broker.reconcile_positions(["SOLUSDT", "ADAUSDT"]), 1)
            # Ни одного вызова set_leverage / set_margin_type!
            paths = [c[1] for c in transport.calls]
            self.assertNotIn("/fapi/v1/leverage", paths)
            self.assertNotIn("/fapi/v1/marginType", paths)

            # Закрытие LONG в HEDGE Mode: отменяет ТОЛЬКО orderId=501 (LONG), не трогает 502 (SHORT), без reduceOnly!
            transport.calls.clear()
            self.assertTrue(broker.submit_close_market("SOLUSDT", reason="SIGNAL_EXIT"))
            batch_call = [c for c in transport.calls if c[1] == "/fapi/v1/batchOrders"][0]
            self.assertEqual(batch_call[2]["orderIdList"], "[501]")
            mkt_call = [c for c in transport.calls if c[1] == "/fapi/v1/order"][0]
            self.assertEqual(mkt_call[2]["positionSide"], "LONG")
            self.assertEqual(mkt_call[2]["side"], "SELL")
            self.assertNotIn("reduceOnly", mkt_call[2])

            # Открытие LONG с HEDGE STOP_MARKET
            transport.calls.clear()
            plan = OrderPlan("SOLUSDT", "LONG", 1.0, 200.0, 175.0, None, 1_700_000_000_000, 200.0)
            self.assertTrue(broker.submit_market_with_bracket(plan))
            self.assertEqual(transport.calls[0][2]["positionSide"], "LONG")
            self.assertEqual(transport.calls[1][2]["type"], "STOP_MARKET")
            self.assertEqual(transport.calls[1][2]["positionSide"], "LONG")
            self.assertEqual(transport.calls[1][2]["closePosition"], "true")
            self.assertEqual(analytics.state["trades_total"], 1)

    def test_ipc_master_slave_snapshot_and_failover(self) -> None:
        async def _scenario() -> None:
            port = _free_port()
            with tempfile.TemporaryDirectory() as tmp:
                lock = Path(tmp) / "streamer.lock"
                received_a: List[PriceTick] = []
                received_b: List[PriceTick] = []

                async def _fake_upstream(syms: List[str], cb, stop_ev: asyncio.Event) -> None:
                    await asyncio.sleep(0.05)
                    await cb(PriceTick("SOLUSDT", 201.5, 1_700_000_000_100))
                    while not stop_ev.is_set():
                        await asyncio.sleep(0.05)

                hub_a = ClavaCrossProcessPriceHub(
                    ["SOLUSDT"], [], lock, host="127.0.0.1", port=port,
                    upstream_runner=_fake_upstream, initial_prices={"SOLUSDT": 200.0},
                )
                hub_b = ClavaCrossProcessPriceHub(
                    ["SOLUSDT"], [], lock, host="127.0.0.1", port=port,
                    upstream_runner=_fake_upstream, initial_prices={"SOLUSDT": 200.0},
                )

                task_a = asyncio.create_task(hub_a.run(lambda t: _append_async(received_a, t)))
                await asyncio.wait_for(hub_a.ready.wait(), timeout=3.0)
                self.assertTrue(hub_a.is_master)

                task_b = asyncio.create_task(hub_b.run(lambda t: _append_async(received_b, t)))
                await asyncio.wait_for(hub_b.snapshot_received.wait(), timeout=3.0)
                self.assertFalse(hub_b.is_master)
                self.assertGreaterEqual(len(received_b), 1)

                # Гасим Master A -> Slave B обязан сам стать Master (Failover)
                hub_a.stop()
                await asyncio.wait_for(task_a, timeout=3.0)
                for _ in range(40):
                    if hub_b.is_master:
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(hub_b.is_master)

                hub_b.stop()
                await asyncio.wait_for(task_b, timeout=3.0)

        async def _append_async(lst: List[PriceTick], t: PriceTick) -> None:
            lst.append(t)

        asyncio.run(_scenario())

    def test_clava_paper_tick_sl_funding_and_polymorphic_analytics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            an = LiveAnalyticsManager("paper_alpha", "PAPER", 1000.0, Path(tmp), ["SOLUSDT"])
            st_file = Path(tmp) / "STATS" / "paper_state.json"
            pb = PaperExecutionBroker(1000.0, 2, 0.0005, 0.0001, st_file, analytics=an)

            pb.on_price_tick("SOLUSDT", 200.0, 1_700_000_000_000)
            plan = OrderPlan("SOLUSDT", "LONG", 1.0, 200.0, 180.0, None, 1_700_000_000_000, 200.0)
            self.assertTrue(pb.submit_market_with_bracket(plan))
            self.assertTrue(st_file.exists())

            # Реальный 8h funding на LONG (+0.001 -> -$0.20)
            cf = pb.apply_funding("SOLUSDT", 0.001, 200.0)
            self.assertAlmostEqual(cf, -0.20)

            # Тик пробивает 5*ATR SL (179.0 <= 180.0) -> немедленное закрытие STOP_LOSS и запись в аналитику
            closed = pb.on_price_tick("SOLUSDT", 179.0, 1_700_036_000_000)
            self.assertIsNotNone(closed)
            assert closed is not None
            self.assertEqual(closed.exit_reason, "STOP_LOSS")
            self.assertFalse(pb.has_open_position("SOLUSDT"))
            self.assertEqual(an.state["trades_total"], 1)
            self.assertGreater(an.state["max_sl_exec_pct"], 10.0)
            self.assertAlmostEqual(an.state["hold_avg_h"], 10.0, places=2)
            self.assertTrue(an.json_path.exists())
            self.assertTrue(an.ledger_path.exists())
            self.assertTrue(an.report_path.exists())

            # Проверка рестарта аналитики из JSON
            an2 = LiveAnalyticsManager("paper_alpha", "PAPER", 1000.0, Path(tmp), ["SOLUSDT"])
            self.assertEqual(an2.state["trades_total"], 1)

    def test_multi_account_cluster_build_prod_and_paper(self) -> None:
        prod = build_live_cluster(rest_transport=_FakeHedgeTransport())
        paper = build_paper_cluster(rest_transport=_FakeHedgeTransport())
        self.assertEqual(len(prod.slots), 1)
        self.assertEqual(len(paper.slots), 1)
        self.assertEqual(len(prod.slots[0].passport.symbols), 14)
        self.assertAlmostEqual(prod.slots[0].passport.capital.start_balance_usd, 1000.0)
        self.assertIn("BTCUSDT", prod.price_hub.cluster_symbols)
        self.assertIn("SOLUSDT", prod.price_hub.cluster_symbols)

        # Проверка защиты от дубликата монеты на одном аккаунте (api_key_name)
        with tempfile.TemporaryDirectory() as tmp:
            bad_cfg = StrictConfigLoader.read_json(ROOT_DIR / "ClavaBot" / "CFG" / "app.json")
            bad_cfg["specs_cache"]["path"] = str((ROOT_DIR / "ClavaBot" / "CACHE" / "specifications.json").resolve())
            bad_cfg["paths"]["analytics_dir"] = tmp
            slot1 = dict(bad_cfg["active_strategies"]["markov_alpha_top14_main"])
            bad_cfg["active_strategies"]["dup_same_acc"] = dict(slot1)
            cfg_p = Path(tmp) / "CFG" / "app.json"
            cfg_p.parent.mkdir(parents=True, exist_ok=True)
            cfg_p.write_text(json.dumps(bad_cfg), encoding="utf-8")
            with self.assertRaises(ValueError):
                ClavaClusterRuntime.from_app_config(cfg_p, ROOT_DIR / "ClavaBot", "PROD")

            # На другом api_key_name (мультиаккаунт) дубликат монет разрешён!
            bad_cfg["active_strategies"]["dup_same_acc"]["api_key_name"] = "API_KEY_ACC2"
            bad_cfg["active_strategies"]["dup_same_acc"]["api_secret_name"] = "API_SECRET_ACC2"
            cfg_p.write_text(json.dumps(bad_cfg), encoding="utf-8")
            multi = ClavaClusterRuntime.from_app_config(cfg_p, ROOT_DIR / "ClavaBot", "PROD")
            self.assertEqual(len(multi.slots), 2)


if __name__ == "__main__":
    unittest.main()
