# Path: ClavaBot/CORE/EXECUTION/cluster_runtime.py
# Role: Единый мультиаккаунтный рантайм для ClavaBot (PROD) и ClavaPaper (PAPER) поверх общего IPC-стрима

from __future__ import annotations

import asyncio
import dataclasses
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ClavaBot.ANALYTICS.live_analytics import LiveAnalyticsManager
from ClavaBot.c_log import UnifiedLogger
from ClavaBot.CORE.candle_aggregator import CandleAggregator, timeframe_to_ms
from ClavaBot.CORE.config_loader import StrategyPassport, StrictConfigLoader
from ClavaBot.CORE.DATA.feature_timeline import LiveRingFeatureTimeline
from ClavaBot.CORE.EXECUTION.base_broker import ExecutionBroker
from ClavaBot.CORE.EXECUTION.hron_binance_broker import (
    HronBinanceConfig,
    HronBinanceProdBroker,
    HronBinanceRestAdapter,
    load_dotenv_file,
)
from ClavaBot.CORE.EXECUTION.ipc_price_hub import ClavaCrossProcessPriceHub, PriceTick
from ClavaBot.CORE.models import Candle
from ClavaBot.CORE.orchestrator import TradeEngineOrchestrator
from ClavaBot.CORE.spec_manager import SpecManager


@dataclass
class StrategySlot:
    """Один изолированный слот стратегии (со своим аккаунтом/брокером, оркестратором, таймлайном и аналитикой)."""
    slot_id: str
    api_key_name: str
    passport: StrategyPassport
    broker: ExecutionBroker
    orchestrator: TradeEngineOrchestrator
    timeline: LiveRingFeatureTimeline
    analytics: LiveAnalyticsManager
    aggregator: CandleAggregator


class ClavaClusterRuntime:
    """
    Единый полиморфный агрегат для ClavaBot (mode='PROD') и ClavaPaper (mode='PAPER'):
    - мультиаккаунт через active_strategies в app.json (с проверкой уникальности монет на один api_key_name);
    - общий Master/Slave IPC стрим цен с HRON_3 ('кто первый поднялся — того и тапочки');
    - суточное авто-обновление CACHE/specifications.json;
    - 220-дневный REST прогрев + реконсиляция позиций + агрегация тиков в 4H свечи + 8h funding.
    """

    def __init__(
        self,
        mode: str,
        app_cfg: Dict[str, Any],
        app_dir: Path,
        bot_root_dir: Path,
        specs_manager: SpecManager,
        slots: List[StrategySlot],
        price_hub: ClavaCrossProcessPriceHub,
        public_rest: HronBinanceRestAdapter,
    ):
        self.logger = UnifiedLogger(f"ClavaRuntime[{mode}]")
        self.mode = mode.upper().strip()
        self.app_cfg = app_cfg
        self.app_dir = Path(app_dir)
        self.bot_root_dir = Path(bot_root_dir)
        self.spec_manager = specs_manager
        self.slots = slots
        self.price_hub = price_hub
        self.public_rest = public_rest
        self._last_funding_bucket: int = -1
        self._last_spec_check_mono: float = time.monotonic()

    @classmethod
    def from_app_config(
        cls,
        app_cfg_path: Path,
        bot_root_dir: Path,
        mode: str,
        broker_factory: Optional[Any] = None,
        rest_transport: Optional[Any] = None,
        upstream_runner: Optional[Any] = None,
    ) -> "ClavaClusterRuntime":
        mode_u = mode.upper().strip()
        app_cfg_path = Path(app_cfg_path).resolve()
        app_dir = app_cfg_path.parent.parent
        bot_root_dir = Path(bot_root_dir).resolve()

        # Загружаем .env из корня проекта ClavaTrading (если существует)
        load_dotenv_file(bot_root_dir.parent / ".env")
        app_cfg = StrictConfigLoader.read_json(app_cfg_path)

        # 1. Загружаем спецификации из отдельного JSON-кэша (раз в 24ч)
        sc = app_cfg["specs_cache"]
        cache_path = (app_dir / str(sc["path"])).resolve()
        spec_manager = SpecManager.from_cache_file(
            cache_path=cache_path,
            ttl_sec=float(sc["ttl_sec"]),
            auto_refresh=False,
            exchange_info_url=str(sc["exchange_info_url"]),
        )

        # 2. Собираем активные стратегии (мультиаккаунт как в HRON_3)
        strategies_dir = (bot_root_dir / str(app_cfg["paths"]["strategies_dir"])).resolve()
        analytics_dir = (app_dir / str(app_cfg["paths"]["analytics_dir"])).resolve()
        bf_cfg = HronBinanceConfig.from_dict(app_cfg["binance_futures"])
        active_map: Dict[str, Any] = app_cfg["active_strategies"]

        slots: List[StrategySlot] = []
        used_by_account: Dict[str, set[str]] = {}
        all_local_symbols: set[str] = set()

        for slot_id, s_cfg in active_map.items():
            if not isinstance(s_cfg, dict) or not bool(s_cfg["enabled"]):
                continue
            passport_file = str(s_cfg["passport"])
            base_passport = StrictConfigLoader.load_strategy_passport(strategies_dir / passport_file)

            override_syms = [str(x).upper().strip() for x in s_cfg["symbols"]]
            start_bal = float(s_cfg["start_balance_usd"])
            risk_ratio = float(s_cfg["risk_per_trade_ratio"])
            lev_budget = int(s_cfg["leverage_budget"])
            api_key_name = str(s_cfg["api_key_name"]).strip()
            api_secret_name = str(s_cfg["api_secret_name"]).strip()

            # Проверка мультиаккаунта: одна монета не должна дублироваться в двух стратегиях одного аккаунта
            acc_set = used_by_account.setdefault(api_key_name, set())
            dups = acc_set.intersection(override_syms)
            if dups:
                raise ValueError(
                    f"Duplicate symbols {sorted(dups)} on account '{api_key_name}'. "
                    f"Укажите разные api_key_name в active_strategies."
                )
            acc_set.update(override_syms)

            passport = dataclasses.replace(
                base_passport,
                strategy_id=str(slot_id),
                symbols=override_syms,
                capital=dataclasses.replace(
                    base_passport.capital,
                    start_balance_usd=start_bal,
                    leverage=lev_budget,
                    risk_per_trade_ratio=risk_ratio,
                    fixed_notional_usd=None,
                ),
            )
            StrictConfigLoader._validate_passport(passport)

            analytics = LiveAnalyticsManager(
                strategy_id=str(slot_id),
                mode=mode_u,
                start_balance_usd=start_bal,
                storage_dir=analytics_dir,
                symbols=override_syms,
            )

            rest_adapter = HronBinanceRestAdapter(
                cfg=bf_cfg,
                api_key_name=api_key_name,
                api_secret_name=api_secret_name,
                transport=rest_transport,
            )
            if broker_factory is not None:
                broker = broker_factory(slot_id, passport, spec_manager, analytics, app_cfg, app_dir)
            else:
                broker = HronBinanceProdBroker(
                    cfg=bf_cfg,
                    spec_manager=spec_manager,
                    rest_adapter=rest_adapter,
                    start_balance_usd=start_bal,
                    analytics=analytics,
                )

            orchestrator = TradeEngineOrchestrator(
                passport=passport,
                spec_manager=spec_manager,
                broker=broker,
            )
            bench = passport.market_sentinel.benchmark_symbol
            tracked = list(override_syms)
            if bench and bench not in tracked:
                tracked.append(bench)
            all_local_symbols.update(tracked)

            timeline = LiveRingFeatureTimeline(symbols=tracked)
            aggregator = CandleAggregator(passport.timeframe)
            slots.append(
                StrategySlot(
                    slot_id=str(slot_id),
                    api_key_name=api_key_name,
                    passport=passport,
                    broker=broker,
                    orchestrator=orchestrator,
                    timeline=timeline,
                    analytics=analytics,
                    aggregator=aggregator,
                )
            )

        if not slots:
            raise ValueError(f"No enabled strategies found in {app_cfg_path}['active_strategies']")

        # 3. Общий Master/Slave IPC стрим цен с HRON_3
        ps = app_cfg["price_stream"]
        lock_file = Path(str(ps["hron_ipc_dir"])) / str(ps["lock_name"])
        peer_paths = [Path(str(p)) for p in ps["peer_app_configs"]] + [app_cfg_path]
        price_hub = ClavaCrossProcessPriceHub(
            local_symbols=sorted(all_local_symbols),
            peer_config_paths=peer_paths,
            lock_file=lock_file,
            host=str(ps["host"]),
            port=int(ps["port"]),
            upstream_runner=upstream_runner,
        )
        public_rest = HronBinanceRestAdapter(
            cfg=bf_cfg,
            api_key_name="PUBLIC",
            api_secret_name="PUBLIC",
            api_key="",
            api_secret="",
            transport=rest_transport,
        )
        return cls(
            mode=mode_u,
            app_cfg=app_cfg,
            app_dir=app_dir,
            bot_root_dir=bot_root_dir,
            specs_manager=spec_manager,
            slots=slots,
            price_hub=price_hub,
            public_rest=public_rest,
        )

    def warmup_and_reconcile(self, now_ms: Optional[int] = None) -> int:
        """Обновляет спецификации (если старше 24ч), прогревает 220д 4H свечей и реконсилирует позиции."""
        if self.spec_manager.is_cache_stale():
            try:
                self.spec_manager.refresh_from_exchange(fetcher=self.public_rest.get_exchange_info)
            except Exception as exc:
                self.logger.warning(f"Spec refresh skipped (using existing cache): {exc}")

        bf_cfg = HronBinanceConfig.from_dict(self.app_cfg["binance_futures"])
        ref_now = int(now_ms) if now_ms is not None else int(time.time() * 1000)
        total_bars = 0

        for slot in self.slots:
            tf_ms = timeframe_to_ms(slot.passport.timeframe)
            closed_end_ms = (ref_now // tf_ms) * tf_ms
            start_ms = closed_end_ms - (bf_cfg.warmup_days * 86_400_000)
            bench = slot.passport.market_sentinel.benchmark_symbol
            syms = list(slot.passport.symbols)
            if bench and bench not in syms:
                syms.append(bench)

            events: List[Tuple[int, int, str, Candle]] = []
            for sym in syms:
                rows = self._fetch_klines_range(sym, slot.passport.timeframe, tf_ms, start_ms, closed_end_ms)
                is_alt = 0 if sym == bench else 1
                for c in rows:
                    events.append((c.close_ts_ms, is_alt, sym, c))
            events.sort(key=lambda item: (item[0], item[1], item[2]))

            slot.orchestrator.set_trading_enabled(False)
            for _ts, _alt, sym, candle in events:
                slot.timeline.push_candle(candle)
                if sym == bench:
                    slot.orchestrator.on_benchmark_candle(candle)
                if sym in slot.passport.symbols:
                    slot.orchestrator.on_candle_close(sym, candle)
                total_bars += 1

            if hasattr(slot.broker, "reconcile_positions"):
                slot.broker.reconcile_positions(slot.passport.symbols)
            slot.orchestrator.set_trading_enabled(True)
            self.logger.info(
                f"[{slot.slot_id}] Warmup done ({len(events)} bars, {bf_cfg.warmup_days}d) | trading ENABLED"
            )
        return total_bars

    def _fetch_klines_range(
        self, symbol: str, interval: str, tf_ms: int, start_ms: int, end_ms: int
    ) -> List[Candle]:
        out: List[Candle] = []
        cursor = int(start_ms)
        while cursor < end_ms:
            rows = self.public_rest.get_klines(symbol, interval, start_time_ms=cursor, end_time_ms=end_ms, limit=1500)
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
            nxt = last_open + tf_ms
            if nxt <= cursor or len(rows) < 1500:
                break
            cursor = nxt
        return out

    async def on_price_tick(self, tick: PriceTick) -> None:
        """Обрабатывает живой тик из общего IPC-стрима во всех активных слотах стратегий."""
        sym = tick.symbol.upper().strip()
        # Раз в 8 часов (00:00 / 08:00 / 16:00 UTC) применяем реальный funding rate для Paper и обновляем guard
        f_bucket = tick.event_time_ms // (8 * 3_600_000)
        if self._last_funding_bucket >= 0 and f_bucket > self._last_funding_bucket:
            self._settle_8h_funding()
        self._last_funding_bucket = f_bucket

        # Суточное обновление спецификаций
        if (time.monotonic() - self._last_spec_check_mono) >= 3600.0:
            self._last_spec_check_mono = time.monotonic()
            if self.spec_manager.is_cache_stale():
                try:
                    self.spec_manager.refresh_from_exchange(fetcher=self.public_rest.get_exchange_info)
                except Exception as exc:
                    self.logger.warning(f"Daily spec refresh failed: {exc}")

        for slot in self.slots:
            bench = slot.passport.market_sentinel.benchmark_symbol
            in_pool = sym in slot.passport.symbols
            if not in_pool and sym != bench:
                continue

            if in_pool:
                if hasattr(slot.broker, "on_price_tick"):
                    # PaperExecutionBroker принимает (sym, price, ts_ms), HronBinanceProdBroker принимает (sym, price)
                    try:
                        slot.broker.on_price_tick(sym, tick.price, tick.event_time_ms)  # type: ignore[misc]
                    except TypeError:
                        slot.broker.on_price_tick(sym, tick.price)  # type: ignore[misc]

            closed_candle = slot.aggregator.push_tick(sym, tick.price, tick.event_time_ms)
            if closed_candle is not None:
                fr = self._fetch_latest_funding_safe(sym) if in_pool else None
                if fr is not None:
                    slot.orchestrator.on_funding_rate(sym, fr)
                slot.timeline.push_candle(closed_candle, funding_rate=fr)
                if sym == bench:
                    slot.orchestrator.on_benchmark_candle(closed_candle)
                if in_pool:
                    slot.orchestrator.on_candle_close(sym, closed_candle)
                    slot.analytics.update_open_positions(
                        slot.broker.get_all_open_positions(),
                        getattr(slot.broker, "last_prices", {}),
                        slot.broker.get_equity_usd(),
                    )
                    slot.analytics.flush()

    def _fetch_latest_funding_safe(self, symbol: str) -> Optional[float]:
        try:
            prem = self.public_rest.get_premium_index(symbol)
            if isinstance(prem, dict) and "lastFundingRate" in prem:
                return float(prem["lastFundingRate"])
        except Exception:
            pass
        return None

    def _settle_8h_funding(self) -> None:
        for slot in self.slots:
            for sym in list(slot.broker.get_all_open_positions().keys()):
                fr = self._fetch_latest_funding_safe(sym)
                if fr is None:
                    continue
                slot.orchestrator.on_funding_rate(sym, fr)
                slot.timeline.update_funding(sym, fr)
                if hasattr(slot.broker, "apply_funding"):
                    slot.broker.apply_funding(sym, fr)  # type: ignore[attr-defined]

    async def run_forever(self) -> None:
        self.warmup_and_reconcile()
        await self.price_hub.run(self.on_price_tick)
