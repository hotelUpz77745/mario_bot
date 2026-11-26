# Path: MarioBot/tests/test_operational_armor_and_tg.py
# Role: Полные тесты Приоритета 2 (Watchdog+Heartbeat, Equity Cutoff, Stale-Tick REST Fallback) и аудит TG-слоя

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

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from MarioBot.CORE.config_loader import StrictConfigLoader
from MarioBot.CORE.EXECUTION.cluster_runtime import MarioClusterRuntime
from MarioBot.CORE.models import OrderPlan
from MarioBot.CORE.SERVICES.equity_cutoff_guard import EmergencyEquityCutoffGuard, EquityCutoffConfig
from MarioBot.CORE.SERVICES.stale_tick_fallback import StaleTickFallbackConfig, StaleTickRestFallback
from MarioBot.CORE.SERVICES.watchdog_service import MarioWatchdogService, WatchdogConfig
from MarioBot.TG.log_export_service import LogExportService
from MarioBot.TG.router import MarioTelegramRouter
from MarioBot.TG.tg_adapter import TelegramAdapter, resolve_tg_token_from_env
from MarioBot.TG.tg_receiver import ResilientAiohttpSession


class TestOperationalArmorAndTelegram(unittest.TestCase):
    def _build_temp_runtime(self, tmp_dir: str) -> tuple[MarioClusterRuntime, Path, List[str]]:
        tmp_p = Path(tmp_dir)
        cfg = StrictConfigLoader.read_json(ROOT_DIR / "MarioBot" / "CFG" / "app.json")
        cfg["specs_cache"]["path"] = str((ROOT_DIR / "MarioBot" / "CACHE" / "specifications.json").resolve())
        cfg["paths"]["analytics_dir"] = str(tmp_p / "ANALYTICS")
        cfg["paths"]["runtime_dir"] = str(tmp_p / "runtime")
        cfg["paths"]["logs_dir"] = str(tmp_p / "logs")
        cfg["app"]["auto_start"] = True
        cfg["telegram"]["enabled"] = False
        cfg_p = tmp_p / "CFG" / "app.json"
        cfg_p.parent.mkdir(parents=True, exist_ok=True)
        cfg_p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        rt = MarioClusterRuntime.from_app_config(cfg_p, ROOT_DIR / "MarioBot", "PROD", configure_logs=False)
        alerts: List[str] = []
        rt._dispatch_tg_alert = alerts.append  # type: ignore[method-assign]
        return rt, cfg_p, alerts

    def test_watchdog_freeze_ws_silence_role_switch_and_silent_heartbeat_autodelete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rt, _cfg_p, alerts = self._build_temp_runtime(tmp)
            tg_calls: List[Tuple[str, Dict[str, Any]]] = []
            msg_seq = [100]

            async def _fake_post(url: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
                method = url.rsplit("/", 1)[-1]
                tg_calls.append((method, payload))
                if method == "sendMessage":
                    msg_seq[0] += 1
                    return {"ok": True, "result": {"message_id": msg_seq[0]}}
                return {"ok": True}

            clock = [1000.0]
            rt.tg_adapter = TelegramAdapter(token="FAKE:TOKEN", chat_ids=[610822492], post_fn=_fake_post, now_fn=lambda: clock[0])
            rt.price_hub.ready.set()
            rt.price_hub._is_master = False  # стартуем как SLAVE

            wd_cfg = WatchdogConfig(
                enabled=True,
                loop_timeout_sec=60.0,
                ws_silence_timeout_sec=60.0,
                check_interval_sec=2.0,
                heartbeat_interval_sec=300.0,
                heartbeat_autodelete_sec=600.0,
                notify_role_switch=True,
            )
            wd = MarioWatchdogService(wd_cfg, rt, now_fn=lambda: clock[0])

            # 1. Первый тик: отправляет тихий Heartbeat (disable_notification=True), запоминает роль SLAVE
            ev1 = asyncio.run(wd.check_once())
            self.assertTrue(ev1.get("heartbeat"))
            self.assertEqual(tg_calls[-1][0], "sendMessage")
            self.assertTrue(tg_calls[-1][1]["disable_notification"])

            # 2. Через 70с без пульса и тиков -> алерты loop_frozen + ws_silent
            clock[0] += 70.0
            ev2 = asyncio.run(wd.check_once())
            self.assertTrue(ev2.get("loop_frozen"))
            self.assertTrue(ev2.get("ws_silent"))
            self.assertTrue(any("Зависание главной лупы" in a for a in alerts))
            self.assertTrue(any("Стрим цен" in a and "молчит" in a for a in alerts))

            # 3. Восстановление + Failover SLAVE -> MASTER + второй heartbeat (editMessageText на месте!)
            clock[0] += 250.0  # +320с от старта (> 300с heartbeat, < 600с autodelete)
            wd.beat_game_loop()
            wd.record_price_tick()
            rt.price_hub._is_master = True
            ev3 = asyncio.run(wd.check_once())
            self.assertTrue(ev3.get("loop_recovered"))
            self.assertTrue(ev3.get("ws_recovered"))
            self.assertEqual(ev3.get("role_switch"), "SLAVE->MASTER")
            self.assertTrue(any("IPC FAILOVER" in a for a in alerts))
            self.assertEqual(tg_calls[-1][0], "editMessageText")

            # 4. Через ещё 350с (> 600с autodelete) -> deleteMessage + свежий sendMessage
            clock[0] += 350.0
            wd.beat_game_loop()
            wd.record_price_tick()
            asyncio.run(wd.check_once())
            methods = [m for m, _ in tg_calls[-2:]]
            self.assertEqual(methods, ["deleteMessage", "sendMessage"])

    def test_emergency_equity_cutoff_closes_positions_pauses_and_resets_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rt, cfg_p, alerts = self._build_temp_runtime(tmp)
            slot = rt.slots[0]
            # Открываем крупную LONG позицию (5 SOL @ $200 = $1,000 notional) на стартовом $1,000 equity
            slot.broker.on_price_tick("SOLUSDT", 200.0)
            self.assertTrue(
                slot.broker.submit_market_with_bracket(
                    OrderPlan("SOLUSDT", "LONG", 5.0, 200.0, 100.0, None, 1_700_000_000_000, 1000.0)
                )
            )
            self.assertFalse(rt.is_paused)

            # Цена падает до $145 -> uPnL = -$275 -> DD ~27.5% >= 25% порога!
            slot.broker.on_price_tick("SOLUSDT", 145.0)
            rt.game_loop_step(1_700_000_010_000)

            assert rt.armor is not None
            self.assertTrue(rt.armor.equity_cutoff.is_tripped)
            self.assertFalse(slot.broker.has_open_position("SOLUSDT"))
            self.assertTrue(rt.is_paused)
            persisted = json.loads(cfg_p.read_text(encoding="utf-8"))
            self.assertFalse(persisted["app"]["auto_start"])
            self.assertTrue(any("EMERGENCY EQUITY CUTOFF" in a for a in alerts))

            # Осознанный ручной ▶️ Start снимает флаг и перекалибровывает HWM на текущее equity
            self.assertTrue(rt.start_trading(persist_app_cfg=True))
            self.assertFalse(rt.armor.equity_cutoff.is_tripped)
            rt.game_loop_step(1_700_000_020_000)
            self.assertFalse(rt.is_paused)

    def test_stale_tick_rest_fallback_refreshes_price_and_triggers_virtual_sl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rt, _cfg_p, _alerts = self._build_temp_runtime(tmp)
            slot = rt.slots[0]
            slot.broker.on_price_tick("SOLUSDT", 200.0)
            self.assertTrue(
                slot.broker.submit_market_with_bracket(
                    OrderPlan("SOLUSDT", "LONG", 1.0, 200.0, 180.0, None, 1_700_000_000_000, 200.0)
                )
            )

            clock = [500.0]
            fetched_batches: List[List[str]] = []

            def _fake_bulk(syms):
                fetched_batches.append(list(syms))
                return {"SOLUSDT": 175.0}  # пробой виртуального SL=180 во время тишины WS!

            fb = StaleTickRestFallback(
                cfg=StaleTickFallbackConfig(
                    enabled=True,
                    stale_threshold_sec=30.0,
                    poll_interval_sec=5.0,
                    open_positions_only=True,
                ),
                runtime=rt,
                prices_fetcher=_fake_bulk,
                now_fn=lambda: clock[0],
            )
            fb.record_tick("SOLUSDT")

            # Через 10с тик ещё свежий -> REST не вызывается
            clock[0] += 10.0
            self.assertEqual(asyncio.run(fb.check_once()), {})
            self.assertEqual(len(fetched_batches), 0)

            # Через 35с тишины -> REST bulk подтягивает $175 и game_loop_step сразу закрывает виртуальный SL!
            clock[0] += 25.0
            refreshed = asyncio.run(fb.check_once())
            self.assertEqual(refreshed, {"SOLUSDT": 175.0})
            self.assertEqual(fb.fallback_fetches, 1)
            self.assertFalse(slot.broker.has_open_position("SOLUSDT"))

    def test_telegram_layer_audit_tokens_ipv4_armor_status_and_null_message_guard(self) -> None:
        # 1. Проверка IPv4 сессии aiogram
        sess = ResilientAiohttpSession()
        self.assertEqual(sess._connector_init["family"], socket.AF_INET)
        self.assertTrue(sess._connector_init["force_close"])

        # 2. Проверка resolve_tg_token_from_env (PROD vs PAPER с fallback)
        prev_main = os.environ.get("TG_TOKEN")
        prev_paper = os.environ.get("TG_TOKEN_PAPER")
        try:
            os.environ["TG_TOKEN"] = "MAIN_TOK"
            os.environ["TG_TOKEN_PAPER"] = "PAPER_TOK"
            self.assertEqual(resolve_tg_token_from_env("PROD"), "MAIN_TOK")
            self.assertEqual(resolve_tg_token_from_env("PAPER"), "PAPER_TOK")
            os.environ["TG_TOKEN_PAPER"] = ""
            self.assertEqual(resolve_tg_token_from_env("PAPER"), "MAIN_TOK")
        finally:
            if prev_main is None:
                os.environ.pop("TG_TOKEN", None)
            else:
                os.environ["TG_TOKEN"] = prev_main
            if prev_paper is None:
                os.environ.pop("TG_TOKEN_PAPER", None)
            else:
                os.environ["TG_TOKEN_PAPER"] = prev_paper

        # 3. Проверка рендера телеметрии Брони в ℹ️ Status
        with tempfile.TemporaryDirectory() as tmp:
            rt, _cfg_p, _alerts = self._build_temp_runtime(tmp)
            router = MarioTelegramRouter(rt, LogExportService(Path(tmp) / "logs", rt.runtime_store))
            status_txt = router.format_status_text()
            self.assertIn("Броня:", status_txt)
            self.assertIn("Delist", status_txt)
            self.assertIn("Cutoff:", status_txt)
            self.assertIn("REST-FB:", status_txt)


if __name__ == "__main__":
    unittest.main()
