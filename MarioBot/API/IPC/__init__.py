# Path: MarioBot/API/IPC/__init__.py
# Role: Межпроцессный транспорт цен Master/Slave для кластера MarioBot + MarioPaper

from MarioBot.API.IPC.price_hub import (
    MarioCrossProcessPriceHub,
    UpstreamRunnerFn,
    discover_cluster_symbols_from_configs,
)

__all__ = [
    "MarioCrossProcessPriceHub",
    "UpstreamRunnerFn",
    "discover_cluster_symbols_from_configs",
]
