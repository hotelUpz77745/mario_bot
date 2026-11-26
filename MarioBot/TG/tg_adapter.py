# Path: MarioBot/TG/tg_adapter.py
# Role: Адаптер Telegram Bot API для алертов (IPv4 aiohttp, токен строго из .env)

from __future__ import annotations

import asyncio
import os
import socket
from typing import Any, Dict, List, Optional, Sequence, Union
import aiohttp

from MarioBot.c_log import UnifiedLogger


def resolve_tg_token_from_env(mode: str) -> str:
    """Читает токен строго из .env: TG_TOKEN_PAPER (fallback TG_TOKEN) для PAPER, TG_TOKEN для PROD."""
    mode_u = mode.upper().strip()
    if mode_u == "PAPER":
        return (os.environ.get("TG_TOKEN_PAPER") or os.environ.get("TG_TOKEN") or "").strip()
    return (os.environ.get("TG_TOKEN") or "").strip()


class TelegramAdapter:
    """Асинхронный адаптер отправки HTML-алертов в Telegram с IPv4-сессией (по образцу HRON_3)."""

    def __init__(self, token: str, chat_ids: Sequence[Union[int, str]] = ()):
        self.logger = UnifiedLogger("TelegramAdapter")
        self.token = str(token or "").strip()
        self.chat_ids: List[Union[int, str]] = list(chat_ids)
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock: Optional[asyncio.Lock] = None

    @staticmethod
    def _is_silent() -> bool:
        return os.environ.get("MARIO_BACKTEST", "0") == "1"

    @property
    def send_message_url(self) -> str:
        return f"https://api.telegram.org/bot{self.token}/sendMessage"

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

    async def send_message(self, chat_id: Union[int, str], text: str, parse_mode: str = "HTML") -> bool:
        if self._is_silent() or not self.token:
            return False
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        try:
            session = await self._get_session()
            async with session.post(self.send_message_url, json=payload) as resp:
                if resp.status == 200:
                    return True
                err = await resp.text()
                self.logger.warning(f"[TG] sendMessage HTTP {resp.status}: {err[:200]}")
                return False
        except Exception as exc:
            self.logger.warning(f"[TG] sendMessage error: {exc}")
            await self.aclose()
            return False

    async def send_alert(self, text: str) -> None:
        if self._is_silent() or not self.token or not self.chat_ids:
            return
        for cid in self.chat_ids:
            await self.send_message(cid, text)
