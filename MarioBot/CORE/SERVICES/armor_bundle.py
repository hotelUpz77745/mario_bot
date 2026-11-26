# Path: MarioBot/CORE/SERVICES/armor_bundle.py
# Role: Единый фасад эксплуатационной брони MarioBot/MarioPaper (Delisting + Watchdog + EquityCutoff + StaleFallback)

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

from MarioBot.CORE.SERVICES.delisting_detector import DelistingDetectorConfig, MarioDelistingDetector
from MarioBot.CORE.SERVICES.equity_cutoff_guard import EmergencyEquityCutoffGuard, EquityCutoffConfig
from MarioBot.CORE.SERVICES.stale_tick_fallback import StaleTickFallbackConfig, StaleTickRestFallback
from MarioBot.CORE.SERVICES.watchdog_service import MarioWatchdogService, WatchdogConfig


class MarioOperationalArmor:
    """
    Чистый фасад всех 4 контуров эксплуатационной защиты кластера:
    1. Двуконтурный авто-детектор делистинга (exchangeInfo + Binance CMS);
    2. Watchdog + тихий Telegram Heartbeat (зависание 0.1s лупы, тишина WS, failover MASTER<->SLAVE);
    3. Emergency Equity Cutoff (стоп-кран по просадке от HWM);
    4. Stale-Tick REST Fallback (страховочная подтяжка цен при тишине WS > 30с).
    """

    def __init__(
        self,
        delisting: MarioDelistingDetector,
        watchdog: MarioWatchdogService,
        equity_cutoff: EmergencyEquityCutoffGuard,
        stale_fallback: StaleTickRestFallback,
    ):
        self.delisting = delisting
        self.watchdog = watchdog
        self.equity_cutoff = equity_cutoff
        self.stale_fallback = stale_fallback

    @classmethod
    def from_app_config(
        cls,
        app_cfg: Dict[str, Any],
        runtime: Any,
        instruments_fetcher: Callable[[], List[Dict[str, Any]]],
        prices_fetcher: Callable[[Sequence[str]], Dict[str, float]],
        cms_client: Optional[Any],
    ) -> "MarioOperationalArmor":
        delisting = MarioDelistingDetector(
            cfg=DelistingDetectorConfig.from_dict(app_cfg["delisting_detector"]),
            runtime=runtime,
            instruments_fetcher=instruments_fetcher,
            cms_client=cms_client,
        )
        watchdog = MarioWatchdogService(
            cfg=WatchdogConfig.from_dict(app_cfg["watchdog"]),
            runtime=runtime,
        )
        equity_cutoff = EmergencyEquityCutoffGuard(
            cfg=EquityCutoffConfig.from_dict(app_cfg["equity_cutoff"]),
            runtime=runtime,
        )
        stale_fallback = StaleTickRestFallback(
            cfg=StaleTickFallbackConfig.from_dict(app_cfg["stale_tick_fallback"]),
            runtime=runtime,
            prices_fetcher=prices_fetcher,
        )
        return cls(delisting=delisting, watchdog=watchdog, equity_cutoff=equity_cutoff, stale_fallback=stale_fallback)

    async def start_all(self) -> None:
        await self.delisting.start()
        await self.watchdog.start()
        await self.stale_fallback.start()

    async def stop_all(self) -> None:
        await self.stale_fallback.stop()
        await self.watchdog.stop()
        await self.delisting.stop()

    def on_price_tick(self, symbol: str) -> None:
        self.watchdog.record_price_tick()
        self.stale_fallback.record_tick(symbol)

    def on_game_loop_step(self, slots: List[Any]) -> List[Dict[str, Any]]:
        self.watchdog.beat_game_loop()
        return self.equity_cutoff.evaluate_slots(slots)

    def on_manual_resume(self, slots: List[Any]) -> None:
        self.equity_cutoff.reset_on_manual_resume(slots)

    def format_status_lines(self, primary_slot: Optional[Any]) -> List[str]:
        dl_tag = "ON (2-contour)" if self.delisting.cfg.enabled else "OFF"
        wd_tag = f"ON (loop {self.watchdog.loop_age_sec:.1f}s)" if self.watchdog.cfg.enabled else "OFF"
        fb_tag = f"ON (fetches={self.stale_fallback.fallback_fetches})" if self.stale_fallback.cfg.enabled else "OFF"
        if not self.equity_cutoff.cfg.enabled:
            ec_tag = "OFF"
        else:
            dd_pct = (self.equity_cutoff.get_slot_drawdown_ratio(primary_slot) * 100.0) if primary_slot else 0.0
            lim_pct = self.equity_cutoff.cfg.max_drawdown_ratio * 100.0
            ec_tag = f"{'TRIPPED' if self.equity_cutoff.is_tripped else 'OK'} ({dd_pct:.1f}%/{lim_pct:.0f}%)"
        return [
            f"🛡️ <b>Броня:</b> Delist <code>{dl_tag}</code> | WD <code>{wd_tag}</code>",
            f"Cutoff: <code>{ec_tag}</code> | REST-FB: <code>{fb_tag}</code>",
        ]
