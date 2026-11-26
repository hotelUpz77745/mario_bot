# Path: MarioBot/TG/tg_adapter.py
# Role: Адаптер Telegram Bot API для алертов и тихого автообновляемого Heartbeat (IPv4 aiohttp, токен строго из .env)

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
import socket
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Union
import aiohttp

from MarioBot.c_log import UnifiedLogger


TgPostFn = Callable[[str, Dict[str, Any]], Awaitable[Optional[Dict[str, Any]]]]


def resolve_tg_token_from_env(mode: str) -> str:
    """Читает токен строго из .env: TG_TOKEN_PAPER (fallback TG_TOKEN) для PAPER, TG_TOKEN для PROD."""
    mode_u = mode.upper().strip()
    if mode_u == "PAPER":
        paper_tok = (os.environ.get("TG_TOKEN_PAPER") or "").strip()
        if paper_tok:
            return paper_tok
        fallback_tok = (os.environ.get("TG_TOKEN") or "").strip()
        if fallback_tok:
            UnifiedLogger("TelegramAdapter").warning(
                "[TG] TG_TOKEN_PAPER не задан в .env — используется fallback TG_TOKEN. "
                "Для одновременного запуска MarioBot + MarioPaper задайте отдельный TG_TOKEN_PAPER."
            )
        return fallback_tok
    return (os.environ.get("TG_TOKEN") or "").strip()


class TelegramAdapter:
    """Асинхронный адаптер HTML-алертов и тихого Heartbeat (edit + autodelete) с IPv4-сессией (по образцу HRON_3)."""

    def __init__(
        self,
        token: str,
        chat_ids: Sequence[Union[int, str]] = (),
        post_fn: Optional[TgPostFn] = None,
        now_fn: Callable[[], float] = time.time,
    ):
        self.logger = UnifiedLogger("TelegramAdapter")
        self.token = str(token or "").strip()
        self.chat_ids: List[Union[int, str]] = list(chat_ids)
        self._post_fn = post_fn
        self._now_fn = now_fn
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock: Optional[asyncio.Lock] = None
        self._heartbeat_msg_id: Optional[int] = None
        self._heartbeat_msg_ts: float = 0.0

    @staticmethod
    def _is_silent() -> bool:
        return os.environ.get("MARIO_BACKTEST", "0") == "1"

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    @property
    def send_message_url(self) -> str:
        return self._api_url("sendMessage")

    @property
    def edit_message_url(self) -> str:
        return self._api_url("editMessageText")

    @property
    def delete_message_url(self) -> str:
        return self._api_url("deleteMessage")

    @staticmethod
    def create_ipv4_session(timeout_sec: float = 15.0) -> aiohttp.ClientSession:
        connector = aiohttp.TCPConnector(family=socket.AF_INET, ttl_dns_cache=300, force_close=True)
        return aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=timeout_sec))

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = self.create_ipv4_session()
            return self._session

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None

    async def _post_json(self, url: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if self._post_fn is not None:
            return await self._post_fn(url, payload)
        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data if isinstance(data, dict) else {"ok": True}
                err = await resp.text()
                self.logger.warning(f"[TG] {url.rsplit('/', 1)[-1]} HTTP {resp.status}: {err[:200]}")
                return None
        except Exception as exc:
            self.logger.warning(f"[TG] {url.rsplit('/', 1)[-1]} error: {exc}")
            await self.aclose()
            return None

    async def send_message(
        self,
        chat_id: Union[int, str],
        text: str,
        parse_mode: str = "HTML",
        disable_notification: bool = False,
    ) -> Optional[int]:
        if self._is_silent() or not self.token:
            return None
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": bool(disable_notification),
        }
        res = await self._post_json(self.send_message_url, payload)
        if not res:
            return None
        msg = res.get("result")
        if isinstance(msg, dict) and "message_id" in msg:
            return int(msg["message_id"])
        return 1

    async def send_alert(self, text: str) -> None:
        if self._is_silent() or not self.token or not self.chat_ids:
            return
        for cid in self.chat_ids:
            await self.send_message(cid, text)

    async def send_or_update_heartbeat(
        self,
        server_name: str,
        autodelete_sec: float = 600.0,
        status_line: str = "",
    ) -> bool:
        """Отправляет тихий Heartbeat, обновляет его на месте и пересоздаёт раз в autodelete_sec (без спама)."""
        if self._is_silent() or not self.token or not self.chat_ids:
            return False
        chat_id = self.chat_ids[0]
        now = self._now_fn()
        utc_now = datetime.fromtimestamp(now, tz=timezone.utc)
        lines = [
            f"🟢 <b>{utc_now.strftime('%Y-%m-%d')}</b> | <b>{server_name}</b>",
            status_line or "Running smoothly.",
            f"🕒 <code>{utc_now.strftime('%H:%M:%S UTC')}</code>",
        ]
        msg_text = "\n".join(lines)

        if self._heartbeat_msg_id is not None and (now - self._heartbeat_msg_ts) >= autodelete_sec:
            await self._post_json(
                self.delete_message_url,
                {"chat_id": chat_id, "message_id": self._heartbeat_msg_id},
            )
            self._heartbeat_msg_id = None

        if self._heartbeat_msg_id is not None:
            edited = await self._post_json(
                self.edit_message_url,
                {
                    "chat_id": chat_id,
                    "message_id": self._heartbeat_msg_id,
                    "text": msg_text,
                    "parse_mode": "HTML",
                },
            )
            if edited is not None:
                return True
            self._heartbeat_msg_id = None

        msg_id = await self.send_message(chat_id, msg_text, parse_mode="HTML", disable_notification=True)
        if msg_id is not None:
            self._heartbeat_msg_id = msg_id
            self._heartbeat_msg_ts = now
            return True
        return False
