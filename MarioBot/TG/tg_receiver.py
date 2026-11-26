# Path: MarioBot/TG/tg_receiver.py
# Role: Оркестратор aiogram 3 поллинга с IPv4-сессией, автореконнектом и whitelist-проверкой (пустой список = всем)

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Any, List, Optional, Sequence

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramConflictError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, ErrorEvent, Message

from MarioBot.c_log import UnifiedLogger
from MarioBot.TG.log_export_service import LogExportService
from MarioBot.TG.router import MarioTelegramRouter


class ResilientAiohttpSession(AiohttpSession):
    """Telegram HTTP-сессия с принудительным IPv4 (AF_INET) и сбросом зависшего коннектора (как в HRON_3)."""

    def __init__(self, timeout: float = 45.0, **kwargs: Any) -> None:
        super().__init__(timeout=timeout, **kwargs)
        self._connector_init["family"] = socket.AF_INET
        self._connector_init["force_close"] = True

    async def make_request(self, bot: Bot, method: Any, timeout: Optional[int] = None) -> Any:
        try:
            return await super().make_request(bot, method, timeout=timeout)
        except (TelegramNetworkError, asyncio.TimeoutError):
            self._should_reset_connector = True
            raise


class MarioTelegramReceiver:
    """Управляет жизненным циклом Telegram-бота MarioBot / MarioPaper."""

    def __init__(
        self,
        runtime: Any,
        token: str,
        allowed_users: Sequence[int],
        logs_dir: Path,
    ):
        self.logger = UnifiedLogger("MarioTGReceiver")
        self.runtime = runtime
        self.token = str(token or "").strip()
        self.allowed_users: List[int] = [int(u) for u in allowed_users]
        self.log_service = LogExportService(
            logs_dir=logs_dir,
            runtime_store=getattr(runtime, "runtime_store", None),
        )
        self.router_handler = MarioTelegramRouter(runtime=runtime, log_service=self.log_service)
        self.bot: Optional[Bot] = None
        self.dp = Dispatcher(storage=MemoryStorage())
        self._setup_middlewares()
        self._setup_routes()

    def is_user_allowed(self, user_id: int) -> bool:
        """Если allowed_users пуст [] — бот доступен ВСЕМ пользователям."""
        if not self.allowed_users:
            return True
        return int(user_id) in self.allowed_users

    def _setup_middlewares(self) -> None:
        async def check_msg(handler, event: Message, data: dict):
            uid = event.from_user.id if event.from_user else 0
            if not self.is_user_allowed(uid):
                self.logger.warning(f"[TG] Unauthorized message from user_id={uid}")
                return
            return await handler(event, data)

        async def check_cb(handler, event: CallbackQuery, data: dict):
            uid = event.from_user.id if event.from_user else 0
            if not self.is_user_allowed(uid):
                self.logger.warning(f"[TG] Unauthorized callback from user_id={uid}")
                return
            return await handler(event, data)

        self.dp.message.outer_middleware.register(check_msg)
        self.dp.callback_query.outer_middleware.register(check_cb)

    def _setup_routes(self) -> None:
        @self.dp.errors()
        async def handle_errors(event: ErrorEvent):
            if isinstance(event.exception, (TelegramNetworkError, TelegramServerError, TelegramRetryAfter)):
                self.logger.warning(f"[TG] Transient Telegram error: {event.exception}")
                return True
            self.logger.error(f"[TG] Unhandled error: {event.exception}")
            return True

        self.dp.include_router(self.router_handler.router)

    async def start(self) -> None:
        if not self.token:
            self.logger.warning("[TG] Token not set in .env -> Telegram polling skipped.")
            return
        self.logger.info(
            f"[TG] Starting polling | allowed_users={'ALL' if not self.allowed_users else self.allowed_users}"
        )
        backoff_sec = 5
        while True:
            try:
                if self.bot is None:
                    self.bot = Bot(token=self.token, session=ResilientAiohttpSession())
                # Сбрасываем старые зависшие апдейты до старта, чтобы старые клики ▶️ Start / ⏸️ Stop не стреляли при рестарте
                await self.bot.delete_webhook(drop_pending_updates=True)
                await self.dp.start_polling(
                    self.bot,
                    handle_signals=False,
                    allowed_updates=["message", "callback_query"],
                )
                break
            except asyncio.CancelledError:
                break
            except TelegramConflictError as exc:
                self.logger.error(
                    f"[TG] Conflict (409): токен уже используется другим процессом! "
                    f"Задайте отдельные TG_TOKEN и TG_TOKEN_PAPER в .env ({exc}). Повтор через 30с..."
                )
                await self.stop()
                await asyncio.sleep(30)
            except Exception as exc:
                self.logger.warning(f"[TG] Polling error: {exc}. Reconnecting in {backoff_sec}s...")
                await self.stop()
                await asyncio.sleep(backoff_sec)
                backoff_sec = min(backoff_sec * 2, 60)
        await self.stop()

    async def stop(self) -> None:
        try:
            await self.dp.stop_polling()
        except Exception:
            pass
        if self.bot is not None:
            try:
                if self.bot.session:
                    await self.bot.session.close()
            except Exception:
                pass
            self.bot = None
