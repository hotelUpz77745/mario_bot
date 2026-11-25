# Path: ClavaBot/CORE/EXECUTION/live_runner.py
# Role: Боевой раннер Binance Futures (авто-прогрев 220д REST + WebSocket 4H поток + реконсиляция)

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Tuple
import websockets

from ClavaBot.c_log import UnifiedLogger
from ClavaBot.CORE.EXECUTION.binance_futures_broker import (
    BinanceFuturesConfig,
    BinanceFuturesExecutionBroker,
)
from ClavaBot.CORE.models import Candle
from ClavaBot.CORE.orchestrator import TradeEngineOrchestrator


_TF_TO_MS: Dict[str, int] = {
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


class BinanceFuturesLiveRunner:
    """
    Управляет полным жизненным циклом боевого бота на Binance Futures:
    1. Синхронизация времени и биржевых спецификаций (exchangeInfo).
    2. Авто-прогрев >= 220 календарных дней 4H свечей (15 монет + BTCUSDT) в режиме trading_enabled=False.
    3. Реконсиляция открытых позиций/стопов на бирже и настройка плеча/маржи.
    4. Подписка на combined WebSocket kline-поток с защитой от дублей и авто-переподключением.
    """

    def __init__(
        self,
        orchestrator: TradeEngineOrchestrator,
        broker: BinanceFuturesExecutionBroker,
        cfg: BinanceFuturesConfig,
    ):
        self.logger = UnifiedLogger("BinanceLiveRunner")
        self.orchestrator = orchestrator
        self.broker = broker
        self.cfg = cfg
        self.timeframe = orchestrator.passport.timeframe
        self.bar_ms = _TF_TO_MS[self.timeframe]
        self.benchmark_symbol: Optional[str] = (
            orchestrator.passport.market_sentinel.benchmark_symbol
            if orchestrator.passport.market_sentinel.enabled
            and orchestrator.passport.market_sentinel.btc_sentiment_enabled
            else None
        )
        self._last_closed_ts: Dict[str, int] = {}

    def _tracked_symbols(self) -> List[str]:
        syms = list(self.orchestrator.passport.symbols)
        if self.benchmark_symbol and self.benchmark_symbol not in syms:
            syms.append(self.benchmark_symbol)
        return syms

    def fetch_history_candles(self, symbol: str, start_ms: int, end_ms: int) -> List[Candle]:
        out: List[Candle] = []
        cursor = int(start_ms)
        while cursor < end_ms:
            rows = self.broker.client.get_klines(
                symbol=symbol,
                interval=self.timeframe,
                start_time_ms=cursor,
                end_time_ms=end_ms,
                limit=1500,
            )
            if not rows:
                break
            last_open = cursor
            for r in rows:
                open_ms = int(r[0])
                close_ms = int(r[6]) + 1
                if close_ms > end_ms:
                    continue
                out.append(
                    Candle(
                        symbol=symbol.upper(),
                        open_ts_ms=open_ms,
                        close_ts_ms=close_ms,
                        open=float(r[1]),
                        high=float(r[2]),
                        low=float(r[3]),
                        close=float(r[4]),
                        volume=float(r[5]),
                    )
                )
                last_open = open_ms
            next_cursor = last_open + self.bar_ms
            if next_cursor <= cursor or len(rows) < 1500:
                break
            cursor = next_cursor
        return out

    def warmup_and_reconcile(self, now_ms: Optional[int] = None) -> int:
        ref_now = int(now_ms) if now_ms is not None else int(time.time() * 1000)
        closed_end_ms = (ref_now // self.bar_ms) * self.bar_ms
        start_ms = closed_end_ms - (self.cfg.warmup_days * 86_400_000)

        self.broker.client.sync_server_time()
        all_syms = self._tracked_symbols()
        self.broker.sync_symbol_specs(all_syms)
        self.broker.configure_margin_and_leverage(self.orchestrator.passport.symbols)

        # Собираем все закрытые свечи и сортируем хронологически (BTCUSDT идёт первым на каждом баре)
        events: List[Tuple[int, int, str, Candle]] = []
        for sym in all_syms:
            candles = self.fetch_history_candles(sym, start_ms, closed_end_ms)
            is_alt = 0 if sym == self.benchmark_symbol else 1
            for c in candles:
                events.append((c.close_ts_ms, is_alt, sym, c))
        events.sort(key=lambda item: (item[0], item[1], item[2]))

        self.orchestrator.set_trading_enabled(False)
        replayed = 0
        for close_ts, _is_alt, sym, candle in events:
            if sym == self.benchmark_symbol:
                self.orchestrator.on_benchmark_candle(candle)
            if sym in self.orchestrator.passport.symbols:
                self.orchestrator.on_candle_close(sym, candle)
            self._last_closed_ts[sym] = close_ts
            replayed += 1

        self.broker.reconcile_positions(self.orchestrator.passport.symbols)
        self.orchestrator.set_trading_enabled(True)
        self.logger.info(
            f"Warmup complete: {replayed} bars ({self.cfg.warmup_days}d) across {len(all_syms)} symbols; trading ENABLED"
        )
        return replayed

    def build_combined_ws_url(self) -> str:
        streams = "/".join(f"{s.lower()}@kline_{self.timeframe}" for s in self._tracked_symbols())
        return f"{self.cfg.ws_url}/stream?streams={streams}"

    def process_ws_kline_message(self, payload: Dict[str, Any]) -> bool:
        data = payload.get("data", payload)
        if not isinstance(data, dict) or data.get("e") != "kline":
            return False
        k = data.get("k")
        if not isinstance(k, dict) or not bool(k.get("x", False)):
            return False

        sym = str(k["s"]).upper().strip()
        close_ts_ms = int(k["T"]) + 1
        if self._last_closed_ts.get(sym, -1) >= close_ts_ms:
            return False
        self._last_closed_ts[sym] = close_ts_ms

        candle = Candle(
            symbol=sym,
            open_ts_ms=int(k["t"]),
            close_ts_ms=close_ts_ms,
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
        )
        if sym == self.benchmark_symbol:
            self.orchestrator.on_benchmark_candle(candle)
        if sym in self.orchestrator.passport.symbols:
            self._refresh_symbol_funding(sym)
            self.orchestrator.on_candle_close(sym, candle)
        return True

    def _refresh_symbol_funding(self, symbol: str) -> None:
        if not hasattr(self.orchestrator, "on_funding_rate"):
            return
        try:
            prem = self.broker.client.get_premium_index(symbol)
            if "lastFundingRate" in prem:
                self.orchestrator.on_funding_rate(symbol, float(prem["lastFundingRate"]))
        except Exception as exc:
            self.logger.warning(f"Funding refresh skipped for {symbol}: {exc}")

    async def run_forever(self) -> None:
        url = self.build_combined_ws_url()
        backoff_sec = 1.0
        while True:
            try:
                self.logger.info(f"Connecting Binance Futures WS ({len(self._tracked_symbols())} streams)...")
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    backoff_sec = 1.0
                    async for raw in ws:
                        msg = json.loads(raw)
                        if self.cfg.entry_delay_sec > 0:
                            data = msg.get("data", msg)
                            if isinstance(data, dict) and data.get("e") == "kline" and data.get("k", {}).get("x"):
                                await asyncio.sleep(self.cfg.entry_delay_sec)
                        self.process_ws_kline_message(msg)
            except asyncio.CancelledError:
                self.logger.info("Live runner stopped by cancel signal")
                raise
            except Exception as exc:
                self.logger.warning(f"WS disconnected ({exc}); reconnecting in {backoff_sec:.1f}s")
                await asyncio.sleep(backoff_sec)
                backoff_sec = min(60.0, backoff_sec * 2.0)
