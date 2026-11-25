# Path: ClavaBot/main.py
# Role: Точка входа боевого каркаса ClavaBot (с мок-исполнением ордеров)

from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ClavaBot.c_log import UnifiedLogger
from ClavaBot.CORE.candle_aggregator import CandleAggregator
from ClavaBot.CORE.config_loader import StrictConfigLoader
from ClavaBot.CORE.EXECUTION.binance_futures_broker import (
    BinanceFuturesConfig,
    BinanceFuturesExecutionBroker,
)
from ClavaBot.CORE.EXECUTION.live_runner import BinanceFuturesLiveRunner
from ClavaBot.CORE.EXECUTION.mock_live_broker import MockLiveExecutionBroker
from ClavaBot.CORE.orchestrator import TradeEngineOrchestrator
from ClavaBot.CORE.spec_manager import SpecManager


def build_prod_runtime(passport_filename: str | None = None) -> tuple[TradeEngineOrchestrator, CandleAggregator]:
    bot_dir = Path(__file__).resolve().parent
    app_cfg = StrictConfigLoader.read_json(bot_dir / "CFG" / "app.json")

    strategies_dir = bot_dir / str(app_cfg["paths"]["strategies_dir"])
    chosen = passport_filename if passport_filename is not None else str(app_cfg["default_passport"])
    passport = StrictConfigLoader.load_strategy_passport(strategies_dir / chosen)

    spec_manager = SpecManager(app_cfg["symbol_specs"])
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


def build_live_runtime(passport_filename: str | None = None) -> BinanceFuturesLiveRunner:
    """Собирает боевой раннер Binance Futures (dry_run по умолчанию из CFG/app.json)."""
    bot_dir = Path(__file__).resolve().parent
    app_cfg = StrictConfigLoader.read_json(bot_dir / "CFG" / "app.json")

    strategies_dir = bot_dir / str(app_cfg["paths"]["strategies_dir"])
    chosen = passport_filename if passport_filename is not None else str(app_cfg["default_passport"])
    passport = StrictConfigLoader.load_strategy_passport(strategies_dir / chosen)

    spec_manager = SpecManager(app_cfg["symbol_specs"])
    bf_cfg = BinanceFuturesConfig.from_dict(app_cfg["binance_futures"])
    broker = BinanceFuturesExecutionBroker(
        cfg=bf_cfg,
        spec_manager=spec_manager,
        dry_run_balance_usd=passport.capital.start_balance_usd,
    )
    orchestrator = TradeEngineOrchestrator(
        passport=passport,
        spec_manager=spec_manager,
        broker=broker,
    )
    return BinanceFuturesLiveRunner(orchestrator=orchestrator, broker=broker, cfg=bf_cfg)


def main() -> None:
    logger = UnifiedLogger("ClavaBotMain")
    orchestrator, _aggregator = build_prod_runtime()
    logger.info(
        f"ClavaBot initialized | strategy={orchestrator.passport.strategy_id} | "
        f"tf={orchestrator.passport.timeframe} | symbols={orchestrator.passport.symbols}"
    )


if __name__ == "__main__":
    main()
