# Path: MarioBot/CORE/SERVICES/stale_tick_fallback.py
# Role: Stale-Tick REST Fallback — страховочная подтяжка цен /fapi/v1/ticker/price при тишине WS > 30с (без HTTP в CORE)

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from MarioBot.c_log import UnifiedLogger


def _is_backtest() -> bool:
    return os.environ.get("MARIO_BACKTEST") == "1"


@dataclass(frozen=True, slots=True)
class StaleTickFallbackConfig:
    """Строгая конфигурация Stale-Tick REST Fallback из app.json['stale_tick_fallback'] (секунды)."""
    enabled: bool
    stale_threshold_sec: float
    poll_interval_sec: float
    open_positions_only: bool

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "StaleTickFallbackConfig":
        return cls(
            enabled=bool(raw["enabled"]),
            stale_threshold_sec=float(raw["stale_threshold_sec"]),
            poll_interval_sec=float(raw["poll_interval_sec"]),
            open_positions_only=bool(raw["open_positions_only"]),
        )


class StaleTickRestFallback:
    """
    Страховочный REST-фоллбек актуальности цен для виртуальных SL/TP в 0.1s игровой лупе:
    - Фиксирует время последнего живого тика по каждому символу;
    - Если по открытой позиции (или любому отслеживаемому символу при open_positions_only=false)
      тиков нет > stale_threshold_sec (по умолчанию 30с):
      1. За один bulk-запрос (через инъекцию BinanceFuturesRestClient.get_prices_bulk) подтягивает цены;
      2. Скармливает свежие цены в брокеры (broker.on_price_tick) и сразу прогоняет шаг game_loop_step(),
         чтобы виртуальный SL/TP не «ослеп» при обрыве WS или тихой ночи.
    """

    def __init__(
        self,
        cfg: StaleTickFallbackConfig,
        runtime: Any,
        prices_fetcher: Callable[[Sequence[str]], Dict[str, float]],
        now_fn: Callable[[], float] = time.monotonic,
    ):
        self.logger = UnifiedLogger("StaleTickFallback")
        self.cfg = cfg
        self.runtime = runtime
        self._prices_fetcher = prices_fetcher
        self._now_fn = now_fn

        self._task: Optional[asyncio.Task[None]] = None
        self._last_tick_mono: Dict[str, float] = {}
        self.fallback_fetches: int = 0
        self.refreshed_symbols_total: int = 0

    def record_tick(self, symbol: str) -> None:
        self._last_tick_mono[symbol.upper().strip()] = self._now_fn()

    async def start(self) -> None:
        if not self.cfg.enabled or _is_backtest():
            self.logger.info("[STALE_FALLBACK] Отключён (disabled или бэктест).")
            return
        if self._task is not None and not self._task.done():
            return
        self.logger.info(
            f"[STALE_FALLBACK] Запуск: порог тишины={self.cfg.stale_threshold_sec:.0f}с, "
            f"проверка каждые {self.cfg.poll_interval_sec:.0f}с (open_only={self.cfg.open_positions_only})"
        )
        self._task = asyncio.create_task(self._loop(), name="mario_stale_tick_fallback")

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
                self.logger.warning(f"[STALE_FALLBACK] Ошибка фоллбека: {exc}")
            await asyncio.sleep(self.cfg.poll_interval_sec)

    def _collect_candidate_symbols(self) -> List[str]:
        syms = set()
        for slot in getattr(self.runtime, "slots", []):
            if self.cfg.open_positions_only:
                syms.update(slot.broker.get_all_open_positions().keys())
            else:
                syms.update(slot.passport.symbols)
                syms.update(slot.broker.get_all_open_positions().keys())
        return sorted(s.upper().strip() for s in syms if s)

    async def check_once(self) -> Dict[str, float]:
        """Проверяет просроченные символы и при необходимости подтягивает их цены одним REST bulk-вызовом."""
        if not self.cfg.enabled or _is_backtest():
            return {}
        now = self._now_fn()
        candidates = self._collect_candidate_symbols()
        if not candidates:
            return {}

        stale: List[str] = []
        for sym in candidates:
            last_m = self._last_tick_mono.get(sym)
            if last_m is None or (now - last_m) >= self.cfg.stale_threshold_sec:
                stale.append(sym)
        if not stale:
            return {}

        prices = await asyncio.to_thread(self._prices_fetcher, stale)
        if not prices:
            return {}

        self.fallback_fetches += 1
        now_ms = int(time.time() * 1000)
        refreshed: Dict[str, float] = {}
        for sym, raw_px in prices.items():
            px = float(raw_px)
            if px <= 0.0:
                continue
            sym_u = sym.upper().strip()
            refreshed[sym_u] = px
            self._last_tick_mono[sym_u] = self._now_fn()
            for slot in getattr(self.runtime, "slots", []):
                if sym_u in slot.passport.symbols or slot.broker.has_open_position(sym_u):
                    if hasattr(slot.broker, "on_price_tick"):
                        try:
                            slot.broker.on_price_tick(sym_u, px, now_ms)  # type: ignore[misc]
                        except TypeError:
                            slot.broker.on_price_tick(sym_u, px)  # type: ignore[misc]

        if refreshed:
            self.refreshed_symbols_total += len(refreshed)
            self.logger.info(
                f"[STALE_FALLBACK] REST-подтяжка {len(refreshed)}/{len(stale)} просроченных символов "
                f"(всего вызовов={self.fallback_fetches})"
            )
            if hasattr(self.runtime, "game_loop_step"):
                self.runtime.game_loop_step(now_ms=now_ms)
        return refreshed
