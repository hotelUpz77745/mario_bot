# Path: MarioBot/tests/test_delisting_detector.py
# Role: Полные тесты двуконтурного авто-детектора делистинга (exchangeInfo status/deliveryDate + CMS-новости + Фаза 1/2)

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from MarioBot.API.BINANCE.cms_client import BinanceCmsNewsClient
from MarioBot.CORE.config_loader import StrictConfigLoader
from MarioBot.CORE.EXECUTION.cluster_runtime import MarioClusterRuntime
from MarioBot.CORE.models import OrderPlan
from MarioBot.CORE.SERVICES.delisting_detector import (
    DelistingDetectorConfig,
    MarioDelistingDetector,
)


class _FakeCmsTransport:
    def __init__(self, articles: List[Dict[str, Any]], details: Dict[str, Dict[str, Any]]):
        self.articles = articles
        self.details = details
        self.detail_calls: List[str] = []

    def __call__(
        self, method: str, url: str, headers: Dict[str, str], params: Optional[Dict[str, Any]], timeout_sec: float
    ) -> Any:
        p = dict(params or {})
        if "catalog/list/query" in url:
            return {"data": {"articles": self.articles}}
        if "detail/query" in url:
            code = str(p["articleCode"])
            self.detail_calls.append(code)
            return {"data": self.details.get(code)}
        raise AssertionError(f"Unexpected CMS call: {url}")


class TestTwoContourDelistingDetector(unittest.TestCase):
    def _build_temp_runtime(self, tmp_dir: str) -> tuple[MarioClusterRuntime, Path, List[str]]:
        tmp_p = Path(tmp_dir)
        cfg = StrictConfigLoader.read_json(ROOT_DIR / "MarioBot" / "CFG" / "app.json")
        cfg["specs_cache"]["path"] = str((ROOT_DIR / "MarioBot" / "CACHE" / "specifications.json").resolve())
        cfg["paths"]["analytics_dir"] = str(tmp_p / "ANALYTICS")
        cfg["paths"]["runtime_dir"] = str(tmp_p / "runtime")
        cfg["paths"]["logs_dir"] = str(tmp_p / "logs")
        cfg["telegram"]["enabled"] = False
        cfg_p = tmp_p / "CFG" / "app.json"
        cfg_p.parent.mkdir(parents=True, exist_ok=True)
        cfg_p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        rt = MarioClusterRuntime.from_app_config(cfg_p, ROOT_DIR / "MarioBot", "PROD", configure_logs=False)
        alerts: List[str] = []
        rt._dispatch_tg_alert = alerts.append  # type: ignore[method-assign]
        return rt, cfg_p, alerts

    def test_contour1_status_change_triggers_immediate_phase2_close_and_app_json_removal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rt, cfg_p, alerts = self._build_temp_runtime(tmp)
            slot = rt.slots[0]
            # Открываем позицию по SOLUSDT (dry_run=true)
            slot.broker.on_price_tick("SOLUSDT", 180.0)
            self.assertTrue(
                slot.broker.submit_market_with_bracket(
                    OrderPlan("SOLUSDT", "LONG", 1.0, 180.0, 150.0, None, 1_700_000_000_000, 180.0)
                )
            )
            self.assertTrue(slot.broker.has_open_position("SOLUSDT"))

            now_sec = 1_700_000_000.0
            cfg = DelistingDetectorConfig.from_dict(rt.app_cfg["delisting_detector"])
            det = MarioDelistingDetector(
                cfg=cfg,
                runtime=rt,
                instruments_fetcher=lambda: [
                    {"symbol": "SOLUSDT", "status": "SETTLING", "deliveryDate": 4133404800000},
                    {"symbol": "ADAUSDT", "status": "TRADING", "deliveryDate": 4133404800000},
                ],
                cms_client=BinanceCmsNewsClient(transport=_FakeCmsTransport([], {})),
                now_fn=lambda: now_sec,
            )

            actions = asyncio.run(det.check_once())
            self.assertEqual(actions, {"SOLUSDT": "HARD_CLOSE"})
            self.assertFalse(slot.broker.has_open_position("SOLUSDT"))
            self.assertNotIn("SOLUSDT", slot.passport.symbols)
            self.assertIn("ADAUSDT", slot.passport.symbols)

            persisted = json.loads(cfg_p.read_text(encoding="utf-8"))
            self.assertNotIn("SOLUSDT", persisted["active_strategies"][slot.slot_id]["symbols"])
            self.assertTrue(any("DELISTING → HARD CLOSE" in m for m in alerts))

            # Повторный проход по уже закрытому символу — идемпотентный no-op
            self.assertEqual(asyncio.run(det.check_once()), {})

    def test_contour1_delivery_date_phase1_warn_then_phase2_escalation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rt, _cfg_p, alerts = self._build_temp_runtime(tmp)
            clock = [1_700_000_000.0]
            delivery_ms = int((clock[0] + 10 * 3600) * 1000)  # +10 часов (> порога 4 часа)
            cfg = DelistingDetectorConfig.from_dict(rt.app_cfg["delisting_detector"])
            det = MarioDelistingDetector(
                cfg=cfg,
                runtime=rt,
                instruments_fetcher=lambda: [
                    {"symbol": "XRPUSDT", "status": "TRADING", "deliveryDate": delivery_ms},
                ],
                cms_client=BinanceCmsNewsClient(transport=_FakeCmsTransport([], {})),
                now_fn=lambda: clock[0],
            )

            # 10ч до делистинга -> только Фаза 1 (предупреждение), монета остаётся в пуле
            act_10h = asyncio.run(det.check_once())
            self.assertEqual(act_10h, {"XRPUSDT": "WARN"})
            self.assertIn("XRPUSDT", rt.slots[0].passport.symbols)
            self.assertTrue(any("DELISTING WARNING" in m for m in alerts))

            # Повторно на 9ч -> без спама предупреждениями
            clock[0] += 3600.0
            self.assertEqual(asyncio.run(det.check_once()), {})

            # Сдвиг до 2ч до делистинга (<= 4ч) -> эскалация в Фазу 2!
            clock[0] += 7 * 3600.0
            act_2h = asyncio.run(det.check_once())
            self.assertEqual(act_2h, {"XRPUSDT": "HARD_CLOSE"})
            self.assertNotIn("XRPUSDT", rt.slots[0].passport.symbols)
            self.assertTrue(any("DELISTING → HARD CLOSE" in m for m in alerts))

    def test_contour2_cms_news_prefilters_spot_and_caches_article_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rt, _cfg_p, alerts = self._build_temp_runtime(tmp)
            cms_tr = _FakeCmsTransport(
                articles=[
                    {"code": "SPOT_1", "title": "Binance Spot Will Delist ADAUSDT on 2026-12-01"},
                    {"code": "FUT_2", "title": "Binance Futures Will Delist BCHUSDT Perpetual Contract"},
                ],
                details={
                    "FUT_2": {
                        "title": "Binance Futures Will Delist BCHUSDT Perpetual Contract",
                        "body": "Trading for BCHUSDT perpetual will stop.",
                    }
                },
            )
            cfg = DelistingDetectorConfig.from_dict(rt.app_cfg["delisting_detector"])
            det = MarioDelistingDetector(
                cfg=cfg,
                runtime=rt,
                instruments_fetcher=lambda: [],
                cms_client=BinanceCmsNewsClient(transport=cms_tr),
            )

            sig1 = asyncio.run(det.get_delisted_symbols(force_refresh=True))
            sig2 = asyncio.run(det.get_delisted_symbols(force_refresh=True))
            # SPOT_1 отфильтрован по заголовку без скачивания detail; FUT_2 скачан ровно 1 раз за 2 цикла!
            self.assertEqual(cms_tr.detail_calls, ["FUT_2"])
            self.assertIn("BCHUSDT", sig1)
            self.assertEqual(sig1["BCHUSDT"].reason, "NEWS_DELIST")
            self.assertNotIn("ADAUSDT", sig1)
            self.assertIn("BCHUSDT", sig2)

            actions = asyncio.run(det.check_once())
            self.assertEqual(actions, {"BCHUSDT": "HARD_CLOSE"})
            self.assertNotIn("BCHUSDT", rt.slots[0].passport.symbols)
            self.assertTrue(any("NEWS_DELIST" in m for m in alerts))

    def test_ttl_cache_and_backtest_env_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rt, _cfg_p, _alerts = self._build_temp_runtime(tmp)
            calls = [0]

            def _fetch() -> List[Dict[str, Any]]:
                calls[0] += 1
                return [{"symbol": "LINKUSDT", "status": "TRADING", "deliveryDate": 4133404800000}]

            clock = [1_700_000_000.0]
            cfg = DelistingDetectorConfig.from_dict(rt.app_cfg["delisting_detector"])
            det = MarioDelistingDetector(
                cfg=cfg,
                runtime=rt,
                instruments_fetcher=_fetch,
                cms_client=BinanceCmsNewsClient(transport=_FakeCmsTransport([], {})),
                now_fn=lambda: clock[0],
            )
            asyncio.run(det.get_delisted_symbols(force_refresh=False))
            asyncio.run(det.get_delisted_symbols(force_refresh=False))
            self.assertEqual(calls[0], 1)

            # В режиме MARIO_BACKTEST=1 детектор — жёсткий no-op (0 сетевых вызовов)
            prev = os.environ.get("MARIO_BACKTEST")
            os.environ["MARIO_BACKTEST"] = "1"
            try:
                res = asyncio.run(det.get_delisted_symbols(force_refresh=True))
                self.assertEqual(res, {})
                self.assertEqual(calls[0], 1)
            finally:
                if prev is None:
                    os.environ.pop("MARIO_BACKTEST", None)
                else:
                    os.environ["MARIO_BACKTEST"] = prev


if __name__ == "__main__":
    unittest.main()
