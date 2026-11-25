# Path: ClavaBot/CORE/EXECUTION/ipc_price_hub.py
# Role: Межпроцессный Master/Slave стрим цен (совместимый с HRON_3/cron3/API/IPC/streamer_hub.py, failover + кластер монет)

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
import aiohttp

from ClavaBot.c_log import UnifiedLogger


@dataclass(frozen=True, slots=True)
class PriceTick:
    """Единый тик цены (совместим по формату с HRON_3 HotPriceTick)."""
    symbol: str
    price: float
    event_time_ms: int


def _is_port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def discover_cluster_symbols_from_configs(
    config_paths: Sequence[Path],
    extra_symbols: Optional[Iterable[str]] = None,
) -> List[str]:
    """
    Собирает объединённый список монет по всем app.json кластера (ClavaBot, ClavaPaper, HRON_3):
    поддерживает поля 'symbols', 'benchmark_symbol' и 'active_strategies.<id>.symbols'.
    """
    collected: Set[str] = set()
    if extra_symbols:
        for s in extra_symbols:
            if isinstance(s, str) and s.strip():
                collected.add(s.strip().upper())

    for raw_p in config_paths:
        cfg_path = Path(raw_p)
        if not cfg_path.exists():
            continue
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue

        for key in ("symbols", "cluster_symbols"):
            syms = data.get(key)
            if isinstance(syms, list):
                for s in syms:
                    if isinstance(s, str) and s.strip():
                        collected.add(s.strip().upper())
            elif isinstance(syms, dict):
                for s in syms.keys():
                    if isinstance(s, str) and s.strip():
                        collected.add(s.strip().upper())

        bench = data.get("benchmark_symbol")
        if isinstance(bench, str) and bench.strip():
            collected.add(bench.strip().upper())

        active_strats = data.get("active_strategies")
        if isinstance(active_strats, dict):
            for _sid, s_cfg in active_strats.items():
                if isinstance(s_cfg, dict) and s_cfg.get("enabled", False):
                    for s in s_cfg.get("symbols", []):
                        if isinstance(s, str) and s.strip():
                            collected.add(s.strip().upper())
                    for side_key in ("LONG", "SHORT"):
                        side_blk = s_cfg.get(side_key)
                        if isinstance(side_blk, dict) and isinstance(side_blk.get("invest_size"), dict):
                            for s in side_blk["invest_size"].keys():
                                if isinstance(s, str) and s.strip():
                                    collected.add(s.strip().upper())

    collected.add("BNBUSDT")
    collected.add("BTCUSDT")
    return sorted(collected)


UpstreamRunnerFn = Callable[[List[str], Callable[[PriceTick], Awaitable[None]], asyncio.Event], Awaitable[None]]


async def _binance_ws_upstream(
    symbols: List[str],
    on_tick: Callable[[PriceTick], Awaitable[None]],
    stop_event: asyncio.Event,
) -> None:
    """Поток тиков Binance Futures (<sym>@trade) через aiohttp с chunking и reconnect (как в HRON_3)."""
    ws_base = "wss://fstream.binance.com"
    chunk_size = 80
    chunks = [symbols[i : i + chunk_size] for i in range(0, len(symbols), chunk_size)]

    async def _run_chunk(session: aiohttp.ClientSession, chunk: List[str]) -> None:
        streams = "/".join(f"{s.lower()}@trade" for s in chunk)
        url = f"{ws_base}/stream?streams={streams}"
        backoff = 1.0
        while not stop_event.is_set():
            try:
                async with session.ws_connect(url, autoping=True, heartbeat=15.0, receive_timeout=30.0) as ws:
                    backoff = 1.0
                    async for msg in ws:
                        if stop_event.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            payload = json.loads(msg.data)
                            data = payload.get("data") if isinstance(payload, dict) else None
                            if not isinstance(data, dict):
                                continue
                            sym = str(data.get("s", "")).upper().strip()
                            px = float(data.get("p", 0.0) or 0.0)
                            ts = int(data.get("E", int(time.time() * 1000)) or int(time.time() * 1000))
                            if sym and px > 0.0:
                                await on_tick(PriceTick(symbol=sym, price=px, event_time_ms=ts))
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                            break
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(min(25.0, backoff) * (0.7 + random.random() * 0.6))
                backoff = min(25.0, backoff * 1.7)

    async with aiohttp.ClientSession() as session:
        tasks = [asyncio.create_task(_run_chunk(session, ch)) for ch in chunks if ch]
        try:
            await stop_event.wait()
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t


class ClavaCrossProcessPriceHub:
    """
    Межпроцессный стример цен по протоколу HRON_3/cron3/API/IPC/streamer_hub.py:
    - Кто первый поднялся (HRON_3, ClavaBot или ClavaPaper) -> Master на 127.0.0.1:48123 (1 WS к Binance).
    - Остальные -> Slave на 127.0.0.1:48123 (0 WS к Binance; мгновенный снапшот + тики).
    - При отключении Master любой Slave автоматически перехватывает роль Master (Failover).
    """

    def __init__(
        self,
        local_symbols: Sequence[str],
        peer_config_paths: Sequence[Path],
        lock_file: Path,
        host: str = "127.0.0.1",
        port: int = 48123,
        lease_timeout_sec: float = 10.0,
        heartbeat_interval_sec: float = 2.0,
        upstream_runner: Optional[UpstreamRunnerFn] = None,
        initial_prices: Optional[Dict[str, float]] = None,
    ):
        self.logger = UnifiedLogger("ClavaPriceIPC")
        self.local_symbols: Set[str] = {s.upper().strip() for s in local_symbols if s and s.strip()}
        self.cluster_symbols: List[str] = discover_cluster_symbols_from_configs(peer_config_paths, self.local_symbols)
        self.host = str(host)
        self.port = int(port)
        self.lock_file = Path(lock_file)
        self.lease_timeout_sec = float(lease_timeout_sec)
        self.heartbeat_interval_sec = float(heartbeat_interval_sec)
        self._upstream_runner: UpstreamRunnerFn = upstream_runner if upstream_runner is not None else _binance_ws_upstream

        self.pid = os.getpid()
        self._stop = asyncio.Event()
        self.ready = asyncio.Event()
        self.snapshot_received = asyncio.Event()
        self._is_master = False
        self._clients: Set[asyncio.StreamWriter] = set()
        now_ms = int(time.time() * 1000)
        self._latest_prices: Dict[str, Tuple[float, int]] = (
            {s.upper(): (float(p), now_ms) for s, p in initial_prices.items()} if initial_prices else {}
        )

    @property
    def is_master(self) -> bool:
        return self._is_master

    @property
    def latest_prices(self) -> Dict[str, Tuple[float, int]]:
        return dict(self._latest_prices)

    def stop(self) -> None:
        self._stop.set()

    def _try_acquire_lock(self) -> bool:
        if _is_port_in_use(self.host, self.port):
            return False
        try:
            self.lock_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {"pid": self.pid, "lease_until": time.time() + self.lease_timeout_sec, "port": self.port}
            self.lock_file.write_text(json.dumps(payload), encoding="utf-8")
            return True
        except Exception:
            return False

    def _renew_lease(self) -> None:
        if not self._is_master:
            return
        with contextlib.suppress(Exception):
            payload = {"pid": self.pid, "lease_until": time.time() + self.lease_timeout_sec, "port": self.port}
            self.lock_file.write_text(json.dumps(payload), encoding="utf-8")

    def _release_lock(self) -> None:
        if self._is_master and self.lock_file.exists():
            with contextlib.suppress(Exception):
                content = json.loads(self.lock_file.read_text(encoding="utf-8"))
                if int(content.get("pid", 0)) == self.pid:
                    self.lock_file.unlink(missing_ok=True)
            self._is_master = False

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set() and self._is_master:
            self._renew_lease()
            await asyncio.sleep(self.heartbeat_interval_sec)

    async def _handle_slave_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._clients.add(writer)
        try:
            if self._latest_prices:
                lines = [f"{sym} {p} {ts}\n" for sym, (p, ts) in self._latest_prices.items()]
                writer.write("".join(lines).encode("utf-8"))
            writer.write(f"SNAPSHOT_DONE 0 {int(time.time() * 1000)}\n".encode("utf-8"))
            await writer.drain()
            while not self._stop.is_set():
                data = await reader.readline()
                if not data:
                    break
        except Exception:
            pass
        finally:
            self._clients.discard(writer)
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _broadcast_tick(self, tick: PriceTick) -> None:
        if not self._clients:
            return
        payload = f"{tick.symbol} {tick.price} {tick.event_time_ms}\n".encode("utf-8")
        dead: List[asyncio.StreamWriter] = []
        for w in list(self._clients):
            try:
                w.write(payload)
                await w.drain()
            except Exception:
                dead.append(w)
        for dw in dead:
            self._clients.discard(dw)

    async def _run_master(self, on_tick: Callable[[PriceTick], Awaitable[None]]) -> None:
        server = None
        hb_task = None
        up_task = None
        try:
            try:
                server = await asyncio.start_server(self._handle_slave_client, self.host, self.port)
            except OSError:
                self._release_lock()
                return

            self._is_master = True
            self.logger.info(
                f"[IPC] ACQUIRED MASTER on {self.host}:{self.port} | cluster_symbols={len(self.cluster_symbols)}"
            )
            hb_task = asyncio.create_task(self._heartbeat_loop())

            async def _master_tick_cb(tick: PriceTick) -> None:
                self._latest_prices[tick.symbol] = (tick.price, tick.event_time_ms)
                if tick.symbol in self.local_symbols:
                    await on_tick(tick)
                await self._broadcast_tick(tick)

            up_task = asyncio.create_task(self._upstream_runner(self.cluster_symbols, _master_tick_cb, self._stop))
            self.snapshot_received.set()
            self.ready.set()
            await self._stop.wait()
        finally:
            if hb_task:
                hb_task.cancel()
            if up_task:
                up_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await up_task
            for w in list(self._clients):
                with contextlib.suppress(Exception):
                    w.close()
                    await w.wait_closed()
            self._clients.clear()
            if server:
                server.close()
                await server.wait_closed()
            self._release_lock()
            self.ready.clear()
            self.snapshot_received.clear()

    async def _run_slave(self, on_tick: Callable[[PriceTick], Awaitable[None]]) -> None:
        self._is_master = False
        reader, writer = await asyncio.open_connection(self.host, self.port)
        self.ready.set()
        self.logger.info(f"[IPC] Connected as SLAVE to {self.host}:{self.port} (0 Binance WS)")
        try:
            while not self._stop.is_set():
                line = await reader.readline()
                if not line:
                    self.logger.warning("[IPC] Master EOF -> initiating failover...")
                    break
                parts = line.decode("utf-8", errors="ignore").strip().split()
                if len(parts) < 3:
                    continue
                sym, p_str, ts_str = parts[0], parts[1], parts[2]
                if sym == "SNAPSHOT_DONE":
                    self.snapshot_received.set()
                    continue
                try:
                    tick = PriceTick(symbol=sym.upper(), price=float(p_str), event_time_ms=int(ts_str))
                except ValueError:
                    continue
                self._latest_prices[tick.symbol] = (tick.price, tick.event_time_ms)
                if tick.symbol in self.local_symbols:
                    await on_tick(tick)
        finally:
            self.ready.clear()
            self.snapshot_received.clear()
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def run(self, on_tick: Callable[[PriceTick], Awaitable[None]]) -> None:
        while not self._stop.is_set():
            try:
                if self._try_acquire_lock():
                    await self._run_master(on_tick)
                else:
                    try:
                        await self._run_slave(on_tick)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        if not _is_port_in_use(self.host, self.port):
                            self._release_lock()
                        if not self._stop.is_set():
                            await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                break
