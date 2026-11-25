# Path: ClavaPaper/main.py
# Role: Точка входа бумажного бота ClavaPaper (живые тики IPC, псевдоордера, полиморфная аналитика)

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ClavaBot.c_log import UnifiedLogger
from ClavaBot.CORE.EXECUTION.cluster_runtime import ClavaClusterRuntime
from ClavaPaper.CORE.paper_broker import PaperExecutionBroker


def build_paper_cluster(
    rest_transport: Optional[Any] = None,
    upstream_runner: Optional[Any] = None,
) -> ClavaClusterRuntime:
    paper_dir = Path(__file__).resolve().parent
    bot_root = ROOT_DIR / "ClavaBot"

    def _make_paper_broker(slot_id, passport, _spec_mgr, analytics, app_cfg, app_dir):
        pe = app_cfg["paper_execution"]
        state_path = app_dir / str(app_cfg["paths"]["analytics_dir"]) / "STATS" / f"paper_state_{slot_id}.json"
        return PaperExecutionBroker(
            start_balance_usd=passport.capital.start_balance_usd,
            leverage_budget=passport.capital.leverage,
            taker_fee_rate=float(pe["taker_fee_rate"]),
            slippage_rate=float(pe["slippage_rate"]),
            state_path=state_path,
            analytics=analytics,
        )

    return ClavaClusterRuntime.from_app_config(
        app_cfg_path=paper_dir / "CFG" / "app.json",
        bot_root_dir=bot_root,
        mode="PAPER",
        broker_factory=_make_paper_broker,
        rest_transport=rest_transport,
        upstream_runner=upstream_runner,
    )


def main() -> None:
    logger = UnifiedLogger("ClavaPaperMain")
    runtime = build_paper_cluster()
    logger.info(
        f"ClavaPaper initialized | slots={[s.slot_id for s in runtime.slots]} | "
        f"cluster_symbols={len(runtime.price_hub.cluster_symbols)}"
    )
    if "--run" in sys.argv:
        try:
            asyncio.run(runtime.run_forever())
        except KeyboardInterrupt:
            logger.info("ClavaPaper stopped by Ctrl+C")


if __name__ == "__main__":
    main()
