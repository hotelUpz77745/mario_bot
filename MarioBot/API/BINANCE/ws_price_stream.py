# Path: MarioBot/API/BINANCE/ws_price_stream.py
# Role: Автономный WebSocket-стример живых цен Binance USD-M Futures (<sym>@trade, chunking, ping, backoff)

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import socket
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Iterable, List, Optional

import aiohttp

from MarioBot.c_log import UnifiedLogger


@dataclass(frozen=True, slots=True)
class PriceTick:
    """Единый тик живой цены (совместим по полям с HotPriceTick)."""
    symbol: str
    price: float
    event_time_ms: int


class BinanceHotPriceStream:
    """
    Самостоятельный WebSocket-стример живых цен Binance USD-M Futures:
    - комбинированный поток wss://fstream.binance.com/stream?streams=<sym>@trade/...;
    - чанкинг по chunk_size символов (по умолчанию 80 на сокет);
    - принудительный IPv4, авто-ping, экспоненциальный backoff-реконнект и опциональный throttle_ms;
    - единая ответственность: только приём горячих тиков цены.
    """

    WS_BASE = "wss://fstream.binance.com"

    def __init__(
        self,
        symbols: Iterable[str],
        *,
        chunk_size: int = 80,
        ping_sec: float = 15.0,
        reconnect_min_sec: float = 1.0,
        reconnect_max_sec: float = 25.0,
        throttle_ms: int = 0,
    ):
        self.logger = UnifiedLogger("BinanceHotPriceStream")
        self.symbols: List[str] = sorted({s.upper().strip() for s in symbols if isinstance(s, str) and s.strip()})
        self.chunk_size = max(1, int(chunk_size))
        self.ping_sec = float(ping_sec)
        self.reconnect_min_sec = float(reconnect_min_sec)
        self.reconnect_max_sec = float(reconnect_max_sec)
        self.throttle_ms = int(throttle_ms)

        self._stop = asyncio.Event()
        self.ready = asyncio.Event()
        self._tasks: List[asyncio.Task[None]] = []
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_emit_ms: Dict[str, int] = {}

    def stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        self._stop.set()
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        if self._session is not None and not self._session.closed:
            with contextlib.suppress(Exception):
                await self._session.close()
        self._session = None

    @staticmethod
    def make_url(symbols: List[str]) -> str:
        streams = "/".join(f"{s.lower()}@trade" for s in symbols)
        return f"{BinanceHotPriceStream.WS_BASE}/stream?streams={streams}"

    @staticmethod
    def parse_tick(payload: Dict[str, object]) -> Optional[PriceTick]:
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return None
        sym = str(data.get("s", "")).upper().strip()
        if not sym:
            return None
        try:
            px = float(data.get("p", 0.0) or 0.0)
        except (TypeError, ValueError):
            return None
        if px <= 0.0:
            return None
        try:
            ts = int(data.get("E", int(time.time() * 1000)) or int(time.time() * 1000))
        except (TypeError, ValueError):
            ts = int(time.time() * 1000)
        return PriceTick(symbol=sym, price=px, event_time_ms=ts)

    def _should_emit(self, sym: str, now_ms: int) -> bool:
        if self.throttle_ms <= 0:
            return True
        last = self._last_emit_ms.get(sym, 0)
        if (now_ms - last) >= self.throttle_ms:
            self._last_emit_ms[sym] = now_ms
            return True
        return False

    async def _ping_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.ping_sec)
            if ws.closed:
                break
            try:
                await ws.ping()
            except Exception:
                with contextlib.suppress(Exception):
                    await ws.close()
                break

    async def _run_chunk(
        self,
        chunk: List[str],
        on_tick: Callable[[PriceTick], Awaitable[None]],
    ) -> None:
        url = self.make_url(chunk)
        backoff = self.reconnect_min_sec

        while not self._stop.is_set():
            ping_task: Optional[asyncio.Task[None]] = None
            try:
                assert self._session is not None
                async with self._session.ws_connect(
                    url,
                    autoping=True,
                    heartbeat=self.ping_sec,
                    receive_timeout=30.0,
                    max_msg_size=0,
                ) as ws:
                    backoff = self.reconnect_min_sec
                    self.ready.set()
                    self.logger.info(f"[WS] Connected chunk ({len(chunk)} symbols)")
                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    async for msg in ws:
                        if self._stop.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            payload = json.loads(msg.data)
                            tick = self.parse_tick(payload)
                            if tick is not None and self._should_emit(tick.symbol, tick.event_time_ms):
                                await on_tick(tick)
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                            break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.warning(f"[WS] Reconnecting chunk ({len(chunk)} syms) in {backoff:.1f}s: {exc}")
                jitter = backoff * (0.7 + random.random() * 0.6)
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(min(self.reconnect_max_sec, jitter))
                backoff = min(self.reconnect_max_sec, backoff * 1.7)
            finally:
                if ping_task is not None:
                    ping_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await ping_task

    async def run(self, on_tick: Callable[[PriceTick], Awaitable[None]]) -> None:
        if not self.symbols:
            self.logger.warning("[WS] No symbols provided to BinanceHotPriceStream")
            return
        connector = aiohttp.TCPConnector(family=socket.AF_INET, ttl_dns_cache=300)
        self._session = aiohttp.ClientSession(connector=connector)
        chunks = [self.symbols[i : i + self.chunk_size] for i in range(0, len(self.symbols), self.chunk_size)]
        self._tasks = [asyncio.create_task(self._run_chunk(ch, on_tick)) for ch in chunks if ch]
        try:
            await self._stop.wait()
        finally:
            await self.aclose()


if __name__ == "__main__":
    async def _smoke() -> None:
        stream = BinanceHotPriceStream(["BTCUSDT", "SOLUSDT"])
        cnt = 0

        async def _cb(t: PriceTick) -> None:
            nonlocal cnt
            cnt += 1
            print(f"{t.symbol} {t.price} @ {t.event_time_ms}")
            if cnt >= 5:
                stream.stop()

        await stream.run(_cb)

    asyncio.run(_smoke())
