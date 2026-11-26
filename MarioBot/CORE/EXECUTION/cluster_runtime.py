# Path: MarioBot/CORE/EXECUTION/cluster_runtime.py
# Role: Единый мультиаккаунтный рантайм для MarioBot (PROD) и MarioPaper (PAPER): IPC, 0.1s лупа SL/TP, ресторация, TG

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from MarioBot.ANALYTICS.live_analytics import LiveAnalyticsManager
from MarioBot.API.BINANCE import BinanceFuturesRestClient, BinanceRestConfig, PriceTick
from MarioBot.API.env_loader import load_dotenv_file
from MarioBot.API.IPC import MarioCrossProcessPriceHub
from MarioBot.c_log import UnifiedLogger, configure_file_logging
from MarioBot.CORE.candle_aggregator import CandleAggregator, timeframe_to_ms
from MarioBot.CORE.config_loader import StrategyPassport, StrictConfigLoader
from MarioBot.CORE.DATA.feature_timeline import LiveRingFeatureTimeline
from MarioBot.CORE.EXECUTION.base_broker import ExecutionBroker
from MarioBot.CORE.EXECUTION.binance_broker import BinanceFuturesProdBroker
from MarioBot.CORE.EXECUTION.runtime_state_store import MarioRuntimeStateStore, atomic_write_json
from MarioBot.CORE.models import Candle, SideType
from MarioBot.CORE.orchestrator import TradeEngineOrchestrator
from MarioBot.CORE.spec_manager import SpecManager
from MarioBot.TG.tg_adapter import TelegramAdapter, resolve_tg_token_from_env
from MarioBot.TG.tg_receiver import MarioTelegramReceiver


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


class MarioClusterRuntime:
    """
    Единый полиморфный агрегат для MarioBot (mode='PROD') и MarioPaper (mode='PAPER'):
    - мультиаккаунт через active_strategies с двумя режимами сайзинга ('reinvest' | 'fixed', без плеча в расчётах);
    - автономный Master/Slave IPC стрим цен кластера MarioBot + MarioPaper (MarioBot/API/IPC);
    - главная игровая лупа (app.game_loop_interval_sec = 0.1с) с системными триггерами виртуальных SL и TP;
    - пер-символьный рантайм (CFG/runtime/<slot>/<sym>.json) и простая ресторация позиций перед циклом;
    - Telegram-слой управления (идемпотентные Start/Stop/Status, Analytics 1-й стратегии, Logs + Runtime Backup).
    """

    def __init__(
        self,
        mode: str,
        app_cfg: Dict[str, Any],
        app_cfg_path: Path,
        app_dir: Path,
        bot_root_dir: Path,
        specs_manager: SpecManager,
        runtime_store: MarioRuntimeStateStore,
        slots: List[StrategySlot],
        price_hub: MarioCrossProcessPriceHub,
        public_rest: BinanceFuturesRestClient,
        tg_adapter: Optional[TelegramAdapter] = None,
        tg_receiver: Optional[MarioTelegramReceiver] = None,
    ):
        self.logger = UnifiedLogger(f"MarioRuntime[{mode}]")
        self.mode = mode.upper().strip()
        self.app_cfg = app_cfg
        self.app_cfg_path = Path(app_cfg_path)
        self.app_dir = Path(app_dir)
        self.bot_root_dir = Path(bot_root_dir)
        self.spec_manager = specs_manager
        self.runtime_store = runtime_store
        self.slots = slots
        self.price_hub = price_hub
        self.public_rest = public_rest
        self.tg_adapter = tg_adapter
        self.tg_receiver = tg_receiver

        app_sec = app_cfg["app"]
        self.game_loop_interval_sec: float = float(app_sec["game_loop_interval_sec"])
        self.rest_failsafe_sec: float = float(app_sec["rest_failsafe_sec"])
        tg_enabled = bool(app_cfg["telegram"]["enabled"])
        auto_start = bool(app_sec["auto_start"])
        self.is_paused: bool = (not auto_start) if tg_enabled else False
        self.is_running: bool = False

        self._last_funding_bucket: int = -1
        self._last_spec_check_mono: float = time.monotonic()
        self._alert_loop: Optional[asyncio.AbstractEventLoop] = None

    def _dispatch_tg_alert(self, html_text: str) -> None:
        if self.tg_adapter is None or not self.tg_adapter.token:
            return
        try:
            loop = self._alert_loop or asyncio.get_running_loop()
            loop.create_task(self.tg_adapter.send_alert(html_text))
        except RuntimeError:
            pass

    def start_trading(self, persist_app_cfg: bool = True) -> bool:
        """Идемпотентный запуск торговли: возвращает False, если уже Running."""
        if not self.is_paused:
            return False
        self.is_paused = False
        for slot in self.slots:
            slot.orchestrator.set_trading_enabled(True)
        if persist_app_cfg:
            self._persist_auto_start(True)
        self.logger.info("Trading state switched -> RUNNING")
        return True

    def stop_trading(self, persist_app_cfg: bool = True) -> bool:
        """Идемпотентная пауза торговли: возвращает False, если уже Paused."""
        if self.is_paused:
            return False
        self.is_paused = True
        for slot in self.slots:
            slot.orchestrator.set_trading_enabled(False)
        if persist_app_cfg:
            self._persist_auto_start(False)
        self.logger.info("Trading state switched -> PAUSED (open positions still guarded by virtual SL/TP)")
        return True

    def _persist_auto_start(self, value: bool) -> None:
        try:
            self.app_cfg["app"]["auto_start"] = bool(value)
            atomic_write_json(self.app_cfg_path, self.app_cfg)
        except Exception as exc:
            self.logger.warning(f"Failed to persist app.auto_start={value}: {exc}")

    @classmethod
    def from_app_config(
        cls,
        app_cfg_path: Path,
        bot_root_dir: Path,
        mode: str,
        broker_factory: Optional[Any] = None,
        rest_transport: Optional[Any] = None,
        upstream_runner: Optional[Any] = None,
        configure_logs: bool = True,
    ) -> "MarioClusterRuntime":
        mode_u = mode.upper().strip()
        app_cfg_path = Path(app_cfg_path).resolve()
        app_dir = app_cfg_path.parent.parent
        bot_root_dir = Path(bot_root_dir).resolve()

        # Загружаем .env из корня проекта
        load_dotenv_file(bot_root_dir.parent / ".env")
        app_cfg = StrictConfigLoader.read_json(app_cfg_path)

        # 0. Настройка файлового логирования (logs/all.log с ротацией)
        logs_dir = (app_dir / str(app_cfg["paths"]["logs_dir"])).resolve()
        if configure_logs:
            log_cfg = app_cfg["logging"]
            configure_file_logging(
                logs_dir=logs_dir,
                level=str(log_cfg["level"]),
                max_log_lines=int(log_cfg["max_log_lines"]),
                backup_count=int(log_cfg["backup_count"]),
                log_to_console=bool(log_cfg["log_to_console"]),
                log_to_file=bool(log_cfg["log_to_file"]),
            )

        # 1. Спецификации из суточного кэша
        sc = app_cfg["specs_cache"]
        cache_path = (app_dir / str(sc["path"])).resolve()
        spec_manager = SpecManager.from_cache_file(
            cache_path=cache_path,
            ttl_sec=float(sc["ttl_sec"]),
            auto_refresh=False,
        )

        # 2. Пер-символьный менеджер рантайма (CFG/runtime/<slot>/<sym>.json)
        runtime_dir = (app_dir / str(app_cfg["paths"]["runtime_dir"])).resolve()
        runtime_store = MarioRuntimeStateStore(runtime_dir=runtime_dir)

        # 3. Активные стратегии (мультиаккаунт + двухрежимный сайзинг reinvest/fixed без плеча)
        strategies_dir = (bot_root_dir / str(app_cfg["paths"]["strategies_dir"])).resolve()
        analytics_dir = (app_dir / str(app_cfg["paths"]["analytics_dir"])).resolve()
        bf_cfg = BinanceRestConfig.from_dict(app_cfg["binance_futures"])
        active_map: Dict[str, Any] = app_cfg["active_strategies"]

        tg_cfg = app_cfg["telegram"]
        tg_enabled = bool(tg_cfg["enabled"])
        allowed_users = [int(u) for u in tg_cfg["allowed_users"]]
        tg_token = resolve_tg_token_from_env(mode_u) if tg_enabled else ""
        tg_adapter = TelegramAdapter(token=tg_token, chat_ids=allowed_users) if (tg_enabled and tg_token) else None

        slots: List[StrategySlot] = []
        used_by_account: Dict[str, set[str]] = {}
        all_local_symbols: set[str] = set()

        # Временный колбэк, привяжется к созданному инстансу runtime
        runtime_ref: List[Optional["MarioClusterRuntime"]] = [None]

        def _on_trade_alert(text: str) -> None:
            if runtime_ref[0] is not None:
                runtime_ref[0]._dispatch_tg_alert(text)

        for slot_id, s_cfg in active_map.items():
            if not isinstance(s_cfg, dict) or not bool(s_cfg["enabled"]):
                continue
            passport_file = str(s_cfg["passport"])
            api_key_name = str(s_cfg["api_key_name"]).strip()
            api_secret_name = str(s_cfg["api_secret_name"]).strip()

            passport = StrictConfigLoader.load_strategy_passport(
                strategies_dir / passport_file,
                slot_cfg=s_cfg,
                slot_id=str(slot_id),
            )
            override_syms = list(passport.symbols)
            start_bal = passport.capital.start_balance_usd

            acc_set = used_by_account.setdefault(api_key_name, set())
            dups = acc_set.intersection(override_syms)
            if dups:
                raise ValueError(
                    f"Duplicate symbols {sorted(dups)} on account '{api_key_name}'. "
                    f"Укажите разные api_key_name в active_strategies."
                )
            acc_set.update(override_syms)

            analytics = LiveAnalyticsManager(
                strategy_id=str(slot_id),
                mode=mode_u,
                start_balance_usd=start_bal,
                storage_dir=analytics_dir,
                symbols=override_syms,
            )

            rest_client = BinanceFuturesRestClient(
                cfg=bf_cfg,
                api_key_name=api_key_name,
                api_secret_name=api_secret_name,
                transport=rest_transport,
            )
            if broker_factory is not None:
                broker = broker_factory(
                    slot_id, passport, spec_manager, runtime_store, analytics, app_cfg, app_dir, _on_trade_alert
                )
            else:
                broker = BinanceFuturesProdBroker(
                    cfg=bf_cfg,
                    spec_manager=spec_manager,
                    rest_client=rest_client,
                    start_balance_usd=start_bal,
                    slot_id=str(slot_id),
                    runtime_store=runtime_store,
                    analytics=analytics,
                    on_trade_event=_on_trade_alert,
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

        # 4. Автономный Master/Slave IPC стрим цен кластера MarioBot + MarioPaper (относительные пути!)
        ps = app_cfg["price_stream"]
        raw_ipc_dir = Path(str(ps["ipc_dir"]))
        ipc_dir = raw_ipc_dir if raw_ipc_dir.is_absolute() else (app_dir / raw_ipc_dir).resolve()
        lock_file = ipc_dir / str(ps["lock_name"])
        peer_paths: List[Path] = []
        for raw_p in ps["peer_app_configs"]:
            p_obj = Path(str(raw_p))
            peer_paths.append(p_obj if p_obj.is_absolute() else (app_dir / p_obj).resolve())
        peer_paths.append(app_cfg_path)
        price_hub = MarioCrossProcessPriceHub(
            local_symbols=sorted(all_local_symbols),
            peer_config_paths=peer_paths,
            lock_file=lock_file,
            host=str(ps["host"]),
            port=int(ps["port"]),
            upstream_runner=upstream_runner,
        )
        public_rest = BinanceFuturesRestClient(
            cfg=bf_cfg,
            api_key_name="PUBLIC",
            api_secret_name="PUBLIC",
            api_key="",
            api_secret="",
            transport=rest_transport,
        )

        rt = cls(
            mode=mode_u,
            app_cfg=app_cfg,
            app_cfg_path=app_cfg_path,
            app_dir=app_dir,
            bot_root_dir=bot_root_dir,
            specs_manager=spec_manager,
            runtime_store=runtime_store,
            slots=slots,
            price_hub=price_hub,
            public_rest=public_rest,
            tg_adapter=tg_adapter,
        )
        runtime_ref[0] = rt
        if tg_enabled:
            rt.tg_receiver = MarioTelegramReceiver(
                runtime=rt,
                token=tg_token,
                allowed_users=allowed_users,
                logs_dir=logs_dir,
            )
        return rt

    def _build_fallback_sl_fn(self, slot: StrategySlot):
        atr_mult = float(slot.passport.signal.params.get("atr_sl_mult", 5.0))

        def _fallback(sym: str, side: SideType, entry_px: float) -> Optional[float]:
            snap = slot.timeline.latest(sym)
            if snap is None or snap.atr_14 is None or snap.atr_14 <= 0.0:
                return None
            raw_sl = (entry_px - atr_mult * snap.atr_14) if side == "LONG" else (entry_px + atr_mult * snap.atr_14)
            return self.spec_manager.round_price(sym, max(0.0, raw_sl)) if raw_sl > 0.0 else None

        return _fallback

    def warmup_and_reconcile(self, now_ms: Optional[int] = None) -> int:
        """Обновляет спецификации, прогревает 220д 4H свечей и выполняет ресторацию рантайма."""
        if self.spec_manager.is_cache_stale():
            try:
                self.spec_manager.refresh_from_exchange(fetcher=self.public_rest.get_exchange_info)
            except Exception as exc:
                self.logger.warning(f"Spec refresh skipped (using existing cache): {exc}")

        bf_cfg = BinanceRestConfig.from_dict(self.app_cfg["binance_futures"])
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

            if hasattr(slot.broker, "restore_and_sync"):
                slot.broker.restore_and_sync(slot.passport.symbols, fallback_sl_fn=self._build_fallback_sl_fn(slot))
            elif hasattr(slot.broker, "reconcile_positions"):
                slot.broker.reconcile_positions(slot.passport.symbols)

            slot.orchestrator.set_trading_enabled(not self.is_paused)
            slot.analytics.update_open_positions(
                slot.broker.get_all_open_positions(),
                getattr(slot.broker, "last_prices", {}),
                slot.broker.get_equity_usd(),
            )
            slot.analytics.flush()
            self.logger.info(
                f"[{slot.slot_id}] Warmup+Restore done ({len(events)} bars, {bf_cfg.warmup_days}d) "
                f"| state={'PAUSED' if self.is_paused else 'RUNNING'}"
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
        """Обновляет цены тиков и формирует 4H свечи (виртуальные SL/TP проверяет главная игровая лупа)."""
        sym = tick.symbol.upper().strip()
        f_bucket = tick.event_time_ms // (8 * 3_600_000)
        if self._last_funding_bucket >= 0 and f_bucket > self._last_funding_bucket:
            self._settle_8h_funding()
        self._last_funding_bucket = f_bucket

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

            if in_pool and hasattr(slot.broker, "on_price_tick"):
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

    def game_loop_step(self, now_ms: Optional[int] = None) -> int:
        """Один шаг главной игровой лупы (вызывается каждые 0.1с): системная проверка виртуальных SL/TP."""
        ts_ms = int(now_ms) if now_ms is not None else int(time.time() * 1000)
        triggered = 0
        for slot in self.slots:
            if hasattr(slot.broker, "check_virtual_triggers"):
                closed = slot.broker.check_virtual_triggers(ts_ms)  # type: ignore[attr-defined]
                triggered += len(closed)
        return triggered

    async def _game_loop(self) -> None:
        """Главная игровая лупа с частотой app.game_loop_interval_sec (0.1с): триггерит виртуальные SL/TP."""
        last_flush = time.monotonic()
        while self.is_running:
            try:
                self.game_loop_step()
                now_m = time.monotonic()
                if (now_m - last_flush) >= self.rest_failsafe_sec:
                    last_flush = now_m
                    for slot in self.slots:
                        slot.analytics.update_open_positions(
                            slot.broker.get_all_open_positions(),
                            getattr(slot.broker, "last_prices", {}),
                            slot.broker.get_equity_usd(),
                        )
                        slot.analytics.flush()
            except Exception as exc:
                self.logger.error(f"Game loop error: {exc}")
            await asyncio.sleep(self.game_loop_interval_sec)

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
        self._alert_loop = asyncio.get_running_loop()
        self.is_running = True
        self.warmup_and_reconcile()

        tasks: List[asyncio.Task[Any]] = [
            asyncio.create_task(self.price_hub.run(self.on_price_tick), name="mario_price_hub"),
            asyncio.create_task(self._game_loop(), name="mario_game_loop"),
        ]
        if self.tg_receiver is not None and self.tg_receiver.token:
            tasks.append(asyncio.create_task(self.tg_receiver.start(), name="mario_tg_receiver"))

        try:
            await asyncio.gather(*tasks)
        finally:
            self.is_running = False
            self.price_hub.stop()
            if self.tg_receiver is not None:
                await self.tg_receiver.stop()
            if self.tg_adapter is not None:
                await self.tg_adapter.aclose()
            for t in tasks:
                if not t.done():
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await t
