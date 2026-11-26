# Path: MarioBot/main.py
# Role: Точка входа боевого бота MarioBot (мультиаккаунтный HEDGE рантайм + сухой/мок-сборщик)

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from MarioBot.c_log import UnifiedLogger
from MarioBot.CORE.candle_aggregator import CandleAggregator
from MarioBot.CORE.config_loader import StrictConfigLoader
from MarioBot.CORE.EXECUTION.cluster_runtime import MarioClusterRuntime
from MarioBot.CORE.EXECUTION.mock_live_broker import MockLiveExecutionBroker
from MarioBot.CORE.orchestrator import TradeEngineOrchestrator
from MarioBot.CORE.spec_manager import SpecManager


def build_prod_runtime(passport_filename: str | None = None) -> tuple[TradeEngineOrchestrator, CandleAggregator]:
    """Лёгкая сборка одного оркестратора по 1-му активному слоту из app.json['active_strategies']."""
    bot_dir = Path(__file__).resolve().parent
    app_cfg = StrictConfigLoader.read_json(bot_dir / "CFG" / "app.json")

    if passport_filename is None:
        passport = StrictConfigLoader.load_primary_slot_passport(bot_dir, app_cfg)
    else:
        strategies_dir = bot_dir / str(app_cfg["paths"]["strategies_dir"])
        first_slot = next(
            (v for v in app_cfg["active_strategies"].values() if isinstance(v, dict) and bool(v["enabled"])),
            None,
        )
        passport = StrictConfigLoader.load_strategy_passport(strategies_dir / passport_filename, slot_cfg=first_slot)

    sc = app_cfg["specs_cache"]
    spec_manager = SpecManager.from_cache_file(
        cache_path=(bot_dir / str(sc["path"])).resolve(),
        ttl_sec=float(sc["ttl_sec"]),
        auto_refresh=False,
        exchange_info_url=str(sc["exchange_info_url"]),
    )
    mock_cfg = app_cfg["mock_execution"]
    broker = MockLiveExecutionBroker(
        start_balance_usd=float(mock_cfg["start_balance_usd"]),
        leverage=int(mock_cfg["leverage"]),
        taker_fee_rate=float(mock_cfg["taker_fee_rate"]),
    )
    orchestrator = TradeEngineOrchestrator(
        passport=passport,
        spec_manager=spec_manager,
        broker=broker,
    )
    aggregator = CandleAggregator(passport.timeframe)
    return orchestrator, aggregator


def build_live_cluster(
    rest_transport: Optional[Any] = None,
    upstream_runner: Optional[Any] = None,
) -> MarioClusterRuntime:
    """Собирает боевой мультиаккаунтный HEDGE-рантайм MarioBot с общим IPC-стримом HRON_3."""
    bot_dir = Path(__file__).resolve().parent
    return MarioClusterRuntime.from_app_config(
        app_cfg_path=bot_dir / "CFG" / "app.json",
        bot_root_dir=bot_dir,
        mode="PROD",
        rest_transport=rest_transport,
        upstream_runner=upstream_runner,
    )


def main() -> None:
    logger = UnifiedLogger("MarioBotMain")
    cluster = build_live_cluster()
    logger.info(
        f"MarioBot initialized | slots={[s.slot_id for s in cluster.slots]} | "
        f"dry_run={cluster.app_cfg['binance_futures']['dry_run']} | "
        f"cluster_symbols={len(cluster.price_hub.cluster_symbols)}"
    )
    if "--run" in sys.argv:
        try:
            asyncio.run(cluster.run_forever())
        except KeyboardInterrupt:
            logger.info("MarioBot stopped by Ctrl+C")


if __name__ == "__main__":
    main()




# # Бумажный бот MarioPaper (при auto_start: false стартует на паузе и ждёт ▶️ Start в TG):
# python -m MarioPaper.main --run

# # Боевой бот MarioBot (dry_run: true по умолчанию):
# python -m MarioBot.main --run