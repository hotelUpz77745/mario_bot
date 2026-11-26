# Path: MarioBot/CORE/SERVICES/watchdog_service.py
# Role: Watchdog + Telegram Heartbeat (контроль зависания 0.1s лупы, тишины WS-стрима и переключения MASTER<->SLAVE)

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from MarioBot.c_log import UnifiedLogger


def _is_backtest() -> bool:
    return os.environ.get("MARIO_BACKTEST") == "1"


@dataclass(frozen=True, slots=True)
class WatchdogConfig:
    """Строгая конфигурация Watchdog + Heartbeat из app.json['watchdog'] (единица времени — секунды)."""
    enabled: bool
    loop_timeout_sec: float
    ws_silence_timeout_sec: float
    check_interval_sec: float
    heartbeat_interval_sec: float
    heartbeat_autodelete_sec: float
    notify_role_switch: bool

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "WatchdogConfig":
        return cls(
            enabled=bool(raw["enabled"]),
            loop_timeout_sec=float(raw["loop_timeout_sec"]),
            ws_silence_timeout_sec=float(raw["ws_silence_timeout_sec"]),
            check_interval_sec=float(raw["check_interval_sec"]),
            heartbeat_interval_sec=float(raw["heartbeat_interval_sec"]),
            heartbeat_autodelete_sec=float(raw["heartbeat_autodelete_sec"]),
            notify_role_switch=bool(raw["notify_role_switch"]),
        )


class MarioWatchdogService:
    """
    Фоновый контроллер жизнеспособности MarioBot / MarioPaper (порт идей HRON_3/watchdog.py):
    1. Контролирует пульс главной 0.1s игровой лупы (алерт при зависании > loop_timeout_sec и при восстановлении).
    2. Контролирует тишину стрима цен WS/IPC (алерт при отсутствии тиков > ws_silence_timeout_sec и при восстановлении).
    3. Отслеживает переключения IPC роли (MASTER <-> SLAVE) и уведомляет оператора.
    4. Отправляет тихий Telegram Heartbeat с автообновлением/автоудалением (без спама в чате).
    """

    def __init__(
        self,
        cfg: WatchdogConfig,
        runtime: Any,
        now_fn: Callable[[], float] = time.monotonic,
    ):
        self.logger = UnifiedLogger("MarioWatchdog")
        self.cfg = cfg
        self.runtime = runtime
        self._now_fn = now_fn

        now = self._now_fn()
        self._task: Optional[asyncio.Task[None]] = None
        self._last_loop_beat: float = now
        self._last_tick_mono: float = now
        self._loop_ticks: int = 0
        self._price_ticks: int = 0
        self._loop_frozen_alerted: bool = False
        self._ws_silent_alerted: bool = False
        self._last_ipc_role: Optional[str] = None
        self._last_heartbeat_mono: float = 0.0

    def beat_game_loop(self) -> None:
        self._last_loop_beat = self._now_fn()
        self._loop_ticks += 1

    def record_price_tick(self) -> None:
        self._last_tick_mono = self._now_fn()
        self._price_ticks += 1

    @property
    def loop_age_sec(self) -> float:
        return max(0.0, self._now_fn() - self._last_loop_beat)

    @property
    def ws_silence_sec(self) -> float:
        return max(0.0, self._now_fn() - self._last_tick_mono)

    async def start(self) -> None:
        if not self.cfg.enabled or _is_backtest():
            self.logger.info("[WATCHDOG] Отключён (disabled или бэктест).")
            return
        if self._task is not None and not self._task.done():
            return
        now = self._now_fn()
        self._last_loop_beat = now
        self._last_tick_mono = now
        self._last_heartbeat_mono = 0.0
        self.logger.info(
            f"[WATCHDOG] Запуск: loop_timeout={self.cfg.loop_timeout_sec:.0f}s, "
            f"ws_timeout={self.cfg.ws_silence_timeout_sec:.0f}s, hb={self.cfg.heartbeat_interval_sec:.0f}s"
        )
        self._task = asyncio.create_task(self._loop(), name="mario_watchdog")

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.error(f"[WATCHDOG] Ошибка итерации: {exc}")
            await asyncio.sleep(self.cfg.check_interval_sec)

    def _server_label(self) -> str:
        app_name = str(self.runtime.app_cfg.get("app", {}).get("name", "MarioBot"))
        mode = str(getattr(self.runtime, "mode", "PROD"))
        return f"{app_name} [{mode}]"

    def _notify(self, html_text: str) -> None:
        if hasattr(self.runtime, "_dispatch_tg_alert"):
            self.runtime._dispatch_tg_alert(html_text)

    async def check_once(self) -> Dict[str, Any]:
        """Выполняет один контрольный проход Watchdog (удобен для детерминированных юнит-тестов)."""
        events: Dict[str, Any] = {}
        if not self.cfg.enabled or _is_backtest():
            return events

        now = self._now_fn()
        label = self._server_label()

        # 1. Контроль зависания главной 0.1s игровой лупы
        loop_diff = max(0.0, now - self._last_loop_beat)
        if loop_diff > self.cfg.loop_timeout_sec:
            if not self._loop_frozen_alerted:
                self._loop_frozen_alerted = True
                events["loop_frozen"] = True
                msg = (
                    f"🚨 <b>[WATCHDOG] Зависание главной лупы {label}!</b>\n"
                    f"Нет пульса уже <code>{int(loop_diff)}с</code> (порог <code>{int(self.cfg.loop_timeout_sec)}с</code>)."
                )
                self.logger.error(msg)
                self._notify(msg)
        elif self._loop_frozen_alerted:
            self._loop_frozen_alerted = False
            events["loop_recovered"] = True
            msg = f"✅ <b>[WATCHDOG] Главная лупа {label} восстановилась!</b>"
            self.logger.info(msg)
            self._notify(msg)

        # 2. Контроль тишины стрима цен WS/IPC
        ws_diff = max(0.0, now - self._last_tick_mono)
        if ws_diff > self.cfg.ws_silence_timeout_sec:
            if not self._ws_silent_alerted:
                self._ws_silent_alerted = True
                events["ws_silent"] = True
                msg = (
                    f"⚠️ <b>[WATCHDOG] Стрим цен {label} молчит!</b>\n"
                    f"Нет тиков уже <code>{int(ws_diff)}с</code> (порог <code>{int(self.cfg.ws_silence_timeout_sec)}с</code>). "
                    f"Работает REST-фоллбек."
                )
                self.logger.warning(msg)
                self._notify(msg)
        elif self._ws_silent_alerted:
            self._ws_silent_alerted = False
            events["ws_recovered"] = True
            msg = f"✅ <b>[WATCHDOG] Стрим цен {label} восстановлен!</b>"
            self.logger.info(msg)
            self._notify(msg)

        # 3. Контроль переключения IPC роли (MASTER <-> SLAVE)
        price_hub = getattr(self.runtime, "price_hub", None)
        if self.cfg.notify_role_switch and price_hub is not None and getattr(price_hub, "ready", None) is not None:
            if price_hub.ready.is_set():
                cur_role = str(getattr(price_hub, "role", "UNKNOWN"))
                if self._last_ipc_role is not None and cur_role != self._last_ipc_role:
                    events["role_switch"] = f"{self._last_ipc_role}->{cur_role}"
                    msg = (
                        f"🔄 <b>[IPC FAILOVER] {label} сменил роль стримера:</b>\n"
                        f"<code>{self._last_ipc_role}</code> → <b>{cur_role}</b>"
                    )
                    self.logger.warning(msg)
                    self._notify(msg)
                self._last_ipc_role = cur_role

        # 4. Тихий Telegram Heartbeat с автообновлением / автоудалением
        tg = getattr(self.runtime, "tg_adapter", None)
        if (
            tg is not None
            and hasattr(tg, "send_or_update_heartbeat")
            and self.cfg.heartbeat_interval_sec > 0.0
            and (self._last_heartbeat_mono == 0.0 or (now - self._last_heartbeat_mono) >= self.cfg.heartbeat_interval_sec)
        ):
            self._last_heartbeat_mono = now
            events["heartbeat"] = True
            state_tag = "PAUSED" if getattr(self.runtime, "is_paused", False) else "RUNNING"
            ipc_role = str(getattr(price_hub, "role", "INIT")) if price_hub is not None else "N/A"
            open_cnt = sum(s.broker.open_positions_count() for s in getattr(self.runtime, "slots", []))
            extra = f"State: <b>{state_tag}</b> | IPC: <code>{ipc_role}</code> | Open: <code>{open_cnt}</code>"
            await tg.send_or_update_heartbeat(
                server_name=label,
                autodelete_sec=self.cfg.heartbeat_autodelete_sec,
                status_line=extra,
            )

        return events
