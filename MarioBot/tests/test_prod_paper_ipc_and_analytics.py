# Path: MarioBot/tests/test_prod_paper_ipc_and_analytics.py
# Role: Полные тесты MarioBot (HEDGE + виртуальные SL/TP в лупе + ресторация), MarioPaper, IPC failover, TG и логов

from __future__ import annotations

import asyncio
import json
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

from MarioBacktest.ENGINE.time_grid_precompiler import (
    DEFAULT_MARKOV_INDICATOR_SPECS,
    UnifiedTimeGridPrecompiler,
)
from MarioBot.ANALYTICS.live_analytics import LiveAnalyticsManager
from MarioBot.c_log import LineRotatingFileHandler
from MarioBot.CORE.config_loader import StrictConfigLoader
from MarioBot.CORE.DATA.feature_timeline import LiveRingFeatureTimeline, PrecompiledFeatureTimeline
from MarioBot.CORE.EXECUTION.cluster_runtime import MarioClusterRuntime
from MarioBot.CORE.EXECUTION.hron_binance_broker import (
    HronBinanceConfig,
    HronBinanceProdBroker,
    HronBinanceRestAdapter,
)
from MarioBot.CORE.EXECUTION.ipc_price_hub import MarioCrossProcessPriceHub, PriceTick
from MarioBot.CORE.EXECUTION.runtime_state_store import MarioRuntimeStateStore
from MarioBot.CORE.models import OpenPosition, OrderPlan
from MarioBot.CORE.spec_manager import SpecManager
from MarioBot.main import build_live_cluster
from MarioBot.TG.log_export_service import LogExportService
from MarioBot.TG.router import MarioTelegramRouter
from MarioBot.TG.tg_receiver import MarioTelegramReceiver
from MarioPaper.CORE.paper_broker import PaperExecutionBroker
from MarioPaper.main import build_paper_cluster


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _FakeHedgeTransport:
    """Мок REST транспорта Binance Futures (HEDGE Mode, только MARKET ордера — никаких STOP_MARKET!)."""

    def __init__(self, live_positions: Optional[List[Dict[str, Any]]] = None) -> None:
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self.live_positions = (
            live_positions
            if live_positions is not None
            else [
                {"symbol": "SOLUSDT", "positionSide": "LONG", "positionAmt": "3.25", "entryPrice": "182.0"},
                {"symbol": "ADAUSDT", "positionSide": "LONG", "positionAmt": "0", "entryPrice": "0"},
            ]
        )

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
            return {"availableBalance": "5000.00", "positions": self.live_positions}
        if path == "/fapi/v1/order":
            return {"orderId": 901, "executedQty": p["quantity"], "avgPrice": "200.0"}
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

    def test_hron_hedge_broker_virtual_sl_tp_and_simple_runtime_restoration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_cfg = StrictConfigLoader.read_json(ROOT_DIR / "MarioBot" / "CFG" / "app.json")
            bf_cfg = HronBinanceConfig.from_dict({**app_cfg["binance_futures"], "dry_run": False})
            store = MarioRuntimeStateStore(Path(tmp) / "runtime")
            # Предзаписываем рантайм: SOLUSDT (qty=2.0 -> на бирже 3.25) и ADAUSDT (на бирже 0 -> сброс в дефолт!)
            store.save_symbol_position(
                "slot1", "SOLUSDT", OpenPosition("SOLUSDT", "LONG", 2.0, 180.0, 1_700_000_000_000, 155.0, 260.0, 0.0, 0.1)
            )
            store.save_symbol_position(
                "slot1", "ADAUSDT", OpenPosition("ADAUSDT", "LONG", 100.0, 0.5, 1_700_000_000_000, 0.4, None, 0.0, 0.02)
            )

            transport = _FakeHedgeTransport()
            rest = HronBinanceRestAdapter(bf_cfg, "API_KEY", "API_SECRET", api_key="k", api_secret="s", transport=transport)
            spec_mgr = SpecManager.from_app_config(ROOT_DIR / "MarioBot", app_cfg)
            analytics = LiveAnalyticsManager("slot1", "PROD", 1000.0, Path(tmp) / "an", ["SOLUSDT", "ADAUSDT"])
            broker = HronBinanceProdBroker(
                bf_cfg, spec_mgr, rest, start_balance_usd=1000.0, slot_id="slot1", runtime_store=store, analytics=analytics
            )

            # Простая ресторация: SOL подсосался и скорректировал qty=3.25 (сохранив virtual SL=155 и TP=260), ADA сбросилась в дефолт!
            self.assertEqual(broker.restore_and_sync(["SOLUSDT", "ADAUSDT"]), 1)
            sol_pos = broker.get_open_position("SOLUSDT")
            assert sol_pos is not None
            self.assertAlmostEqual(sol_pos.qty, 3.25)
            self.assertAlmostEqual(sol_pos.stop_loss or 0.0, 155.0)
            self.assertAlmostEqual(sol_pos.take_profit or 0.0, 260.0)
            self.assertFalse(broker.has_open_position("ADAUSDT"))
            ada_disk = json.loads(store.symbol_path("slot1", "ADAUSDT").read_text(encoding="utf-8"))
            self.assertFalse(ada_disk["in_position"])
            self.assertEqual(ada_disk["qty"], 0.0)

            # Ни одного вызова set_leverage / set_margin_type / STOP_MARKET!
            paths = [c[1] for c in transport.calls]
            self.assertNotIn("/fapi/v1/leverage", paths)
            self.assertNotIn("/fapi/v1/marginType", paths)

            # Виртуальный SL триггерится главной лупой при цене <= 155.0 -> отправка ТОЛЬКО MARKET HEDGE SELL
            transport.calls.clear()
            broker.on_price_tick("SOLUSDT", 154.5)
            closed = broker.check_virtual_triggers(1_700_010_000_000)
            self.assertEqual(len(closed), 1)
            self.assertEqual(closed[0].exit_reason, "STOP_LOSS")
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(transport.calls[0][2]["type"], "MARKET")
            self.assertEqual(transport.calls[0][2]["positionSide"], "LONG")
            self.assertEqual(transport.calls[0][2]["side"], "SELL")
            self.assertNotIn("reduceOnly", transport.calls[0][2])

            # Открытие новой позиции: ТОЛЬКО 1 MARKET ордер (никаких физических STOP_MARKET на бирже!)
            transport.calls.clear()
            plan = OrderPlan("SOLUSDT", "LONG", 1.0, 200.0, 175.0, 250.0, 1_700_020_000_000, 200.0)
            self.assertTrue(broker.submit_market_with_bracket(plan))
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(transport.calls[0][2]["type"], "MARKET")
            self.assertEqual(transport.calls[0][2]["positionSide"], "LONG")

            # Виртуальный TP триггерится главной лупой при цене >= 250.0!
            broker.on_price_tick("SOLUSDT", 252.0)
            tp_closed = broker.check_virtual_triggers(1_700_030_000_000)
            self.assertEqual(len(tp_closed), 1)
            self.assertEqual(tp_closed[0].exit_reason, "TAKE_PROFIT")
            self.assertEqual(analytics.state["trades_total"], 2)

            # Проверка экспорта Runtime Backup
            bkp = store.export_backup()
            self.assertTrue(bkp.exists())

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

                hub_a = MarioCrossProcessPriceHub(
                    ["SOLUSDT"], [], lock, host="127.0.0.1", port=port,
                    upstream_runner=_fake_upstream, initial_prices={"SOLUSDT": 200.0},
                )
                hub_b = MarioCrossProcessPriceHub(
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

    def test_mario_paper_game_loop_sl_funding_and_polymorphic_analytics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            an = LiveAnalyticsManager("paper_alpha", "PAPER", 1000.0, Path(tmp) / "an", ["SOLUSDT"])
            store = MarioRuntimeStateStore(Path(tmp) / "runtime")
            pb = PaperExecutionBroker(1000.0, 0.0005, 0.0001, "paper_alpha", store, analytics=an)
            pb.restore_and_sync(["SOLUSDT"])

            pb.on_price_tick("SOLUSDT", 200.0, 1_700_000_000_000)
            plan = OrderPlan("SOLUSDT", "LONG", 1.0, 200.0, 180.0, None, 1_700_000_000_000, 200.0)
            self.assertTrue(pb.submit_market_with_bracket(plan))
            self.assertTrue(store.symbol_path("paper_alpha", "SOLUSDT").exists())

            cf = pb.apply_funding("SOLUSDT", 0.001, 200.0)
            self.assertAlmostEqual(cf, -0.20)

            # Тик пробивает виртуальный SL -> срабатывает в шаге игровой лупы check_virtual_triggers()
            pb.on_price_tick("SOLUSDT", 179.0, 1_700_036_000_000)
            closed = pb.check_virtual_triggers(1_700_036_000_000)
            self.assertEqual(len(closed), 1)
            self.assertEqual(closed[0].exit_reason, "STOP_LOSS")
            self.assertFalse(pb.has_open_position("SOLUSDT"))
            self.assertEqual(an.state["trades_total"], 1)
            self.assertGreater(an.state["max_sl_exec_pct"], 10.0)
            self.assertAlmostEqual(an.state["hold_avg_h"], 10.0, places=2)

    def test_telegram_idempotency_whitelist_analytics_and_log_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            cfg = StrictConfigLoader.read_json(ROOT_DIR / "MarioBot" / "CFG" / "app.json")
            cfg["specs_cache"]["path"] = str((ROOT_DIR / "MarioBot" / "CACHE" / "specifications.json").resolve())
            cfg["paths"]["analytics_dir"] = str(tmp_p / "ANALYTICS")
            cfg["paths"]["runtime_dir"] = str(tmp_p / "runtime")
            cfg["paths"]["logs_dir"] = str(tmp_p / "logs")
            cfg["app"]["auto_start"] = False
            cfg["telegram"]["allowed_users"] = [610822492]
            cfg_p = tmp_p / "CFG" / "app.json"
            cfg_p.parent.mkdir(parents=True, exist_ok=True)
            cfg_p.write_text(json.dumps(cfg), encoding="utf-8")

            rt = MarioClusterRuntime.from_app_config(
                cfg_p, ROOT_DIR / "MarioBot", "PROD", rest_transport=_FakeHedgeTransport(), configure_logs=False
            )
            # При telegram.enabled=True и auto_start=False стартует на паузе!
            self.assertTrue(rt.is_paused)
            self.assertAlmostEqual(rt.game_loop_interval_sec, 0.1)

            # Идемпотентность Start / Stop
            self.assertTrue(rt.start_trading(persist_app_cfg=True))
            self.assertFalse(rt.start_trading(persist_app_cfg=True))  # повторный Start = no-op
            self.assertTrue(rt.stop_trading(persist_app_cfg=True))
            self.assertFalse(rt.stop_trading(persist_app_cfg=True))  # повторный Stop = no-op

            # Whitelist: [610822492] пускает только его; пустой список [] пускает всех!
            rec_strict = MarioTelegramReceiver(rt, token="", allowed_users=[610822492], logs_dir=tmp_p / "logs")
            self.assertTrue(rec_strict.is_user_allowed(610822492))
            self.assertFalse(rec_strict.is_user_allowed(999999))
            rec_open = MarioTelegramReceiver(rt, token="", allowed_users=[], logs_dir=tmp_p / "logs")
            self.assertTrue(rec_open.is_user_allowed(999999))

            # Проверка рендера Status и Analytics 1-й стратегии
            log_svc = LogExportService(logs_dir=tmp_p / "logs", runtime_store=rt.runtime_store)
            router = MarioTelegramRouter(rt, log_svc)
            status_txt = router.format_status_text()
            self.assertIn("Mario Control Panel", status_txt)
            self.assertIn("REINVEST", status_txt)
            an_txt = router.format_primary_analytics_text()
            self.assertIn("markov_alpha_top14_main", an_txt)

            # Проверка ротации logs/all.log и LogExportService
            log_file = tmp_p / "logs" / "all.log"
            handler = LineRotatingFileHandler(log_file, max_lines=100, backup_count=2)
            import logging
            lg = logging.getLogger("mario.test_rot")
            lg.handlers = [handler]
            lg.setLevel(logging.INFO)
            for i in range(130):
                lg.info(f"line-{i}")
            self.assertTrue(log_file.exists())
            self.assertTrue((tmp_p / "logs" / "all.log.1").exists())
            tail_out = tmp_p / "logs" / "tail.log"
            self.assertTrue(LogExportService.extract_log_tail(log_file, tail_out, max_lines=10))
            self.assertEqual(len(tail_out.read_text(encoding="utf-8").splitlines()), 10)

    def test_multi_account_cluster_build_prod_and_paper(self) -> None:
        prod = build_live_cluster(rest_transport=_FakeHedgeTransport())
        paper = build_paper_cluster(rest_transport=_FakeHedgeTransport(), configure_logs=False)
        self.assertEqual(len(prod.slots), 1)
        self.assertEqual(len(paper.slots), 1)
        self.assertEqual(len(prod.slots[0].passport.symbols), 14)
        self.assertAlmostEqual(prod.slots[0].passport.capital.start_balance_usd, 1000.0)
        self.assertEqual(prod.slots[0].passport.capital.sizing_mode, "reinvest")
        self.assertIn("BTCUSDT", prod.price_hub.cluster_symbols)
        self.assertIn("SOLUSDT", prod.price_hub.cluster_symbols)

        with tempfile.TemporaryDirectory() as tmp:
            bad_cfg = StrictConfigLoader.read_json(ROOT_DIR / "MarioBot" / "CFG" / "app.json")
            bad_cfg["specs_cache"]["path"] = str((ROOT_DIR / "MarioBot" / "CACHE" / "specifications.json").resolve())
            bad_cfg["paths"]["analytics_dir"] = str(Path(tmp) / "an")
            bad_cfg["paths"]["runtime_dir"] = str(Path(tmp) / "rt")
            bad_cfg["paths"]["logs_dir"] = str(Path(tmp) / "logs")
            slot1 = dict(bad_cfg["active_strategies"]["markov_alpha_top14_main"])
            bad_cfg["active_strategies"]["dup_same_acc"] = dict(slot1)
            cfg_p = Path(tmp) / "CFG" / "app.json"
            cfg_p.parent.mkdir(parents=True, exist_ok=True)
            cfg_p.write_text(json.dumps(bad_cfg), encoding="utf-8")
            with self.assertRaises(ValueError):
                MarioClusterRuntime.from_app_config(cfg_p, ROOT_DIR / "MarioBot", "PROD", configure_logs=False)

            bad_cfg["active_strategies"]["dup_same_acc"]["api_key_name"] = "API_KEY_ACC2"
            bad_cfg["active_strategies"]["dup_same_acc"]["api_secret_name"] = "API_SECRET_ACC2"
            cfg_p.write_text(json.dumps(bad_cfg), encoding="utf-8")
            multi = MarioClusterRuntime.from_app_config(cfg_p, ROOT_DIR / "MarioBot", "PROD", configure_logs=False)
            self.assertEqual(len(multi.slots), 2)


if __name__ == "__main__":
    unittest.main()
