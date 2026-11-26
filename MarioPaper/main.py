# Path: MarioPaper/main.py
# Role: Точка входа бумажного бота MarioPaper (живые тики IPC, 0.1s лупа SL/TP, рантайм-ресторация, TG, аналитика)

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from MarioBot.c_log import UnifiedLogger
from MarioBot.CORE.EXECUTION.cluster_runtime import MarioClusterRuntime
from MarioPaper.CORE.paper_broker import PaperExecutionBroker


def build_paper_cluster(
    rest_transport: Optional[Any] = None,
    upstream_runner: Optional[Any] = None,
    configure_logs: bool = True,
) -> MarioClusterRuntime:
    paper_dir = Path(__file__).resolve().parent
    bot_root = ROOT_DIR / "MarioBot"

    def _make_paper_broker(slot_id, passport, _spec_mgr, runtime_store, analytics, app_cfg, _app_dir, on_trade_alert):
        pe = app_cfg["paper_execution"]
        return PaperExecutionBroker(
            start_balance_usd=passport.capital.start_balance_usd,
            taker_fee_rate=float(pe["taker_fee_rate"]),
            slippage_rate=float(pe["slippage_rate"]),
            slot_id=str(slot_id),
            runtime_store=runtime_store,
            analytics=analytics,
            on_trade_event=on_trade_alert,
        )

    return MarioClusterRuntime.from_app_config(
        app_cfg_path=paper_dir / "CFG" / "app.json",
        bot_root_dir=bot_root,
        mode="PAPER",
        broker_factory=_make_paper_broker,
        rest_transport=rest_transport,
        upstream_runner=upstream_runner,
        configure_logs=configure_logs,
    )


def main() -> None:
    logger = UnifiedLogger("MarioPaperMain")
    runtime = build_paper_cluster()
    logger.info(
        f"MarioPaper initialized | slots={[s.slot_id for s in runtime.slots]} | "
        f"paused={runtime.is_paused} | cluster_symbols={len(runtime.price_hub.cluster_symbols)}"
    )
    if "--check" in sys.argv:
        return
    try:
        asyncio.run(runtime.run_forever())
    except KeyboardInterrupt:
        logger.info("MarioPaper stopped by Ctrl+C")


if __name__ == "__main__":
    main()



## шпору не трогать!!
# # chmod 600 ssh_key.txt
# # eval "$(ssh-agent -s)" 
# # ssh-add ssh_key.txt
# # git remote set-url origin git@github.com:hotelUpz/uranus_bot.git
# # source .ssh-autostart.sh
# В терминале Git Bash, находясь в папке с проектом:
# source C:/Users/User/Desktop/My_Pro/HP_EliteBook_735_old/WORKSPACE/COMMON/.ssh-autostart.sh

# git push --set-upstream origin master 
# # git config --global push.autoSetupRemote true
# # ssh -T git@github.com 
# # git log -1
 
# # git add .
# # git commit -m "plh37"
# # git push

# # pip install anthropic
# # npm install -g @anthropic-ai/claude-code

# # export ANTHROPIC_API_KEY=...
# taskkill /F /IM python.exe