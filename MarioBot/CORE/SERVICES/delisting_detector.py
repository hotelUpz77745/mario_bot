# Path: MarioBot/CORE/SERVICES/delisting_detector.py
# Role: Двуконтурный авто-детектор делистинга Binance USDT-M (Контур 1: exchangeInfo, Контур 2: CMS-новости; без прямого HTTP)

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set

from MarioBot.c_log import UnifiedLogger


def _is_backtest() -> bool:
    """True, если процесс запущен в режиме бэктеста (полный no-op)."""
    return os.environ.get("MARIO_BACKTEST") == "1"


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class DelistingDetectorConfig:
    """Строгая конфигурация двуконтурного монитора делистинга из app.json['delisting_detector'] (единица времени — секунды)."""
    enabled: bool
    poll_interval_sec: float
    hard_close_sec: float
    delivery_horizon_sec: float
    ms_per_sec: int
    sec_per_hour: float
    notify_telegram: bool
    quote_asset: str
    exchange_active_statuses: frozenset[str]
    news_catalog_id: int
    news_page_no: int
    news_page_size: int
    news_http_timeout_sec: float
    news_keywords: tuple[str, ...]
    news_ticker_regex: str
    news_ticker_stopwords: frozenset[str]

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "DelistingDetectorConfig":
        return cls(
            enabled=bool(raw["enabled"]),
            poll_interval_sec=float(raw["poll_interval_sec"]),
            hard_close_sec=float(raw["hard_close_sec"]),
            delivery_horizon_sec=float(raw["delivery_horizon_sec"]),
            ms_per_sec=int(raw["ms_per_sec"]),
            sec_per_hour=float(raw["sec_per_hour"]),
            notify_telegram=bool(raw["notify_telegram"]),
            quote_asset=str(raw["quote_asset"]).upper().strip(),
            exchange_active_statuses=frozenset(str(s).upper().strip() for s in raw["exchange_active_statuses"]),
            news_catalog_id=int(raw["news_catalog_id"]),
            news_page_no=int(raw["news_page_no"]),
            news_page_size=int(raw["news_page_size"]),
            news_http_timeout_sec=float(raw["news_http_timeout_sec"]),
            news_keywords=tuple(str(k).lower().strip() for k in raw["news_keywords"]),
            news_ticker_regex=str(raw["news_ticker_regex"]),
            news_ticker_stopwords=frozenset(str(w).upper().strip() for w in raw["news_ticker_stopwords"]),
        )


@dataclass(frozen=True, slots=True)
class DelistingSignal:
    """Результат обнаружения угрозы делистинга по одному инструменту активного пула."""
    symbol: str
    reason: str  # "STATUS_CHANGE" | "DELIST_SCHEDULED" | "NEWS_DELIST"
    status: str
    delist_time_sec: float


class MarioDelistingDetector:
    """
    Двуконтурный монитор делистинга Binance USD-M Futures для MarioBot / MarioPaper (порт проверенной механики HRON_3):
    - Контур 1 (exchangeInfo):
      * статус вне exchange_active_statuses -> немедленная Фаза 2 (STATUS_CHANGE);
      * deliveryDate в пределах delivery_horizon_sec -> Фаза 2 при <= hard_close_sec, иначе Фаза 1 (DELIST_SCHEDULED).
    - Контур 2 (Binance CMS News):
      * анонс фьючерсного делистинга (фильтр заголовка + кэш articleCode) -> немедленная Фаза 2 (NEWS_DELIST).
    - Сетевые вызовы приходят СТРОГО через инъекцию из MarioBot/API/BINANCE (нулевой HTTP в CORE).
    """

    def __init__(
        self,
        cfg: DelistingDetectorConfig,
        runtime: Any,
        instruments_fetcher: Callable[[], List[Dict[str, Any]]],
        cms_client: Any,
        now_fn: Callable[[], float] = time.time,
    ):
        self.logger = UnifiedLogger("DelistingDetector")
        self.cfg = cfg
        self.runtime = runtime
        self._instruments_fetcher = instruments_fetcher
        self._cms_client = cms_client
        self._now_fn = now_fn
        self._ticker_re = re.compile(cfg.news_ticker_regex)

        self._task: Optional[asyncio.Task[None]] = None
        self._last_check_ts: Optional[float] = None
        self._cached: Dict[str, DelistingSignal] = {}
        self._warned: Set[str] = set()
        self._closed: Set[str] = set()
        self._parsed_article_codes: Set[str] = set()
        self._news_delisted_symbols: Set[str] = set()

    def _fmt_eta(self, sec: Optional[float]) -> str:
        if sec is None:
            return "неизвестно (новостной сигнал)"
        return f"≈{max(sec, 0.0) / self.cfg.sec_per_hour:.2f}ч"

    async def start(self) -> None:
        if not self.cfg.enabled or _is_backtest():
            self.logger.info("[DELISTING] Монитор делистингов отключён (disabled или бэктест).")
            return
        if self._task is not None and not self._task.done():
            return
        self.logger.info(
            f"[DELISTING] Запуск двуконтурного монитора делистингов "
            f"(интервал={self.cfg.poll_interval_sec:.0f}с, порог={self.cfg.hard_close_sec:.0f}с)"
        )
        self._task = asyncio.create_task(self._loop(), name="mario_delisting_detector")

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self.logger.info("[DELISTING] Остановка монитора делистингов...")
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
                self.logger.error(f"[DELISTING] Ошибка итерации монитора: {exc}")
            await asyncio.sleep(self.cfg.poll_interval_sec)

    async def get_delisted_symbols(self, force_refresh: bool = False) -> Dict[str, DelistingSignal]:
        """Возвращает {symbol: DelistingSignal} по монетам активных слотов (с TTL-кэшем poll_interval_sec)."""
        if not self.cfg.enabled or _is_backtest():
            return {}
        now = self._now_fn()
        if (
            not force_refresh
            and self._last_check_ts is not None
            and (now - self._last_check_ts) < self.cfg.poll_interval_sec
        ):
            return dict(self._cached)
        signals = await self._collect_signals()
        self._cached = signals
        self._last_check_ts = now
        return dict(signals)

    async def _collect_signals(self) -> Dict[str, DelistingSignal]:
        if _is_backtest():
            return {}
        ours: Set[str] = {str(s).upper().strip() for s in self.runtime.get_active_pool_symbols()}
        if not ours:
            return {}

        now_sec = self._now_fn()
        signals: Dict[str, DelistingSignal] = {}

        # Контур 1: Binance Futures /fapi/v1/exchangeInfo (status + deliveryDate)
        try:
            instruments = await asyncio.to_thread(self._instruments_fetcher)
        except Exception as exc:
            self.logger.warning(f"[DELISTING] Ошибка Контура 1 (exchangeInfo): {exc}")
            instruments = []

        if isinstance(instruments, list):
            for item in instruments:
                if not isinstance(item, dict):
                    continue
                sym = str(item.get("symbol", "")).upper().strip()
                if sym not in ours:
                    continue
                status = str(item.get("status", "")).upper().strip()
                if status and status not in self.cfg.exchange_active_statuses:
                    signals[sym] = DelistingSignal(sym, "STATUS_CHANGE", status, now_sec)
                    continue
                delivery_ms = _safe_int(item.get("deliveryDate"))
                if delivery_ms is None:
                    continue
                delivery_sec = delivery_ms / float(self.cfg.ms_per_sec)
                if now_sec < delivery_sec <= (now_sec + self.cfg.delivery_horizon_sec):
                    signals[sym] = DelistingSignal(sym, "DELIST_SCHEDULED", status, delivery_sec)

        # Контур 2: Новостной CMS Binance (приоритетный немедленный сигнал)
        for sym in await self._fetch_news_delistings():
            if sym in ours:
                signals[sym] = DelistingSignal(sym, "NEWS_DELIST", "", now_sec)

        return signals

    async def _fetch_news_delistings(self) -> Set[str]:
        if _is_backtest() or self._cms_client is None:
            return set()
        try:
            articles = await asyncio.to_thread(
                self._cms_client.get_cms_articles,
                self.cfg.news_catalog_id,
                self.cfg.news_page_no,
                self.cfg.news_page_size,
                self.cfg.news_http_timeout_sec,
            )
            if not articles:
                return set(self._news_delisted_symbols)
            for art in articles:
                if not isinstance(art, dict):
                    continue
                code = art.get("code")
                if not code or code in self._parsed_article_codes:
                    continue
                cat_title = str(art.get("title") or "")
                if cat_title and not any(kw in cat_title.lower() for kw in self.cfg.news_keywords):
                    self._parsed_article_codes.add(str(code))
                    continue
                payload = await asyncio.to_thread(
                    self._cms_client.get_cms_article_detail,
                    str(code),
                    self.cfg.news_http_timeout_sec,
                )
                if not payload:
                    continue
                title = str(payload.get("title") or cat_title)
                body = str(payload.get("body") or "")
                self._parsed_article_codes.add(str(code))
                if not any(kw in title.lower() for kw in self.cfg.news_keywords):
                    continue
                quote = self.cfg.quote_asset
                for token in self._ticker_re.findall(f"{title} {body}"):
                    tok_u = token.upper()
                    if tok_u in self.cfg.news_ticker_stopwords:
                        continue
                    base = tok_u[: -len(quote)] if tok_u.endswith(quote) else tok_u
                    if base:
                        self._news_delisted_symbols.add(f"{base}{quote}")
        except Exception as exc:
            self.logger.warning(f"[DELISTING] Ошибка Контура 2 (CMS-новости): {exc}")
        return set(self._news_delisted_symbols)

    async def check_once(self) -> Dict[str, str]:
        """Выполняет один проход проверки и возвращает карту применённых действий {symbol: 'WARN' | 'HARD_CLOSE'}."""
        actions: Dict[str, str] = {}
        signals = await self.get_delisted_symbols(force_refresh=True)
        if not signals:
            return actions
        now_sec = self._now_fn()
        for sym, sig in signals.items():
            if sig.reason == "NEWS_DELIST":
                if await self._escalate(sig, time_left_sec=None):
                    actions[sym] = "HARD_CLOSE"
                continue
            time_left_sec = sig.delist_time_sec - now_sec
            if sig.reason == "STATUS_CHANGE" or time_left_sec <= self.cfg.hard_close_sec:
                if await self._escalate(sig, time_left_sec=time_left_sec):
                    actions[sym] = "HARD_CLOSE"
            else:
                if await self._warn(sig, time_left_sec=time_left_sec):
                    actions[sym] = "WARN"
        return actions

    async def _warn(self, sig: DelistingSignal, time_left_sec: float) -> bool:
        sym = sig.symbol.upper()
        if sym in self._warned or sym in self._closed:
            return False
        self._warned.add(sym)
        self.logger.warning(
            f"[DELISTING][{sym}] Предупреждение ({sig.reason}, status={sig.status}), "
            f"до делистинга {self._fmt_eta(time_left_sec)} (порог {self._fmt_eta(self.cfg.hard_close_sec)})."
        )
        if self.cfg.notify_telegram:
            self._notify(self._build_warn_msg(sig, time_left_sec))
        return True

    async def _escalate(self, sig: DelistingSignal, time_left_sec: Optional[float]) -> bool:
        sym = sig.symbol.upper()
        if sym in self._closed:
            return False
        self._closed.add(sym)
        self._warned.discard(sym)
        self.logger.critical(
            f"[DELISTING][{sym}] ФАЗА 2 — ЖЁСТКОЕ ЗАКРЫТИЕ И ВЫВОД ИЗ ПУЛА! "
            f"reason={sig.reason}, status={sig.status}, eta={self._fmt_eta(time_left_sec)}"
        )
        result = self.runtime.emergency_delist_symbol(sym, reason="DELISTING")
        self.logger.info(f"[DELISTING][{sym}] Итог вывода из кластера: {result}")
        if self.cfg.notify_telegram:
            self._notify(self._build_hard_msg(sig, time_left_sec, result))
        return True

    def _notify(self, html_text: str) -> None:
        if hasattr(self.runtime, "_dispatch_tg_alert"):
            self.runtime._dispatch_tg_alert(html_text)

    def _build_warn_msg(self, sig: DelistingSignal, time_left_sec: float) -> str:
        return (
            f"⚠️ <b>[DELISTING WARNING] Монета {sig.symbol} готовится к делистингу!</b>\n\n"
            f"• Причина: <b>{sig.reason}</b>\n"
            f"• Статус биржи: <code>{sig.status}</code>\n"
            f"• До делистинга: <b>{self._fmt_eta(time_left_sec)}</b> "
            f"(порог жёсткого закрытия: {self._fmt_eta(self.cfg.hard_close_sec)})\n"
            f"• Действие: <b>предупреждение</b>. При достижении порога позиция будет закрыта MARKET и монета удалена из пула."
        )

    def _build_hard_msg(self, sig: DelistingSignal, time_left_sec: Optional[float], res: Dict[str, Any]) -> str:
        return (
            f"🚨 <b>[DELISTING → HARD CLOSE] Монета {sig.symbol} закрыта и выведена из пула!</b>\n\n"
            f"• Причина: <b>{sig.reason}</b>\n"
            f"• Статус биржи: <code>{sig.status}</code>\n"
            f"• До делистинга: <b>{self._fmt_eta(time_left_sec)}</b>\n"
            f"• Закрыто позиций: <code>{res.get('closed_positions', 0)}</code> | "
            f"Удалено из слотов: <code>{res.get('removed_from_slots', [])}</code>\n"
            f"• Конфиг: <code>app.json</code> обновлён атомарно, новые входы по {sig.symbol} заблокированы."
        )
