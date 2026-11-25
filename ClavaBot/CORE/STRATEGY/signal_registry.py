# Path: ClavaBot/CORE/STRATEGY/signal_registry.py
# Role: Реестр инъектируемых сигнальных плагинов стратегий

from __future__ import annotations

from typing import Callable, Dict
from ClavaBot.CORE.config_loader import StrategyPassport
from ClavaBot.CORE.STRATEGY.base_signal_plugin import BaseSignalPlugin
from ClavaBot.CORE.STRATEGY.bb_breakout_signal_plugin import BollingerBreakoutSignalPlugin
from ClavaBot.CORE.STRATEGY.choch_signal_plugin import ChochSignalPlugin
from ClavaBot.CORE.STRATEGY.ema_cross_signal_plugin import EmaCrossSignalPlugin
from ClavaBot.CORE.STRATEGY.ema_pullback_signal_plugin import EmaPullbackSignalPlugin
from ClavaBot.CORE.STRATEGY.gerchik_fp_signal_plugin import GerchikTwoBarFpSignalPlugin
from ClavaBot.CORE.STRATEGY.ict_silver_bullet_plugin import IctSilverBulletSignalPlugin
from ClavaBot.CORE.STRATEGY.macd_anti_signal_plugin import MacdAntiSignalPlugin
from ClavaBot.CORE.STRATEGY.markov_pure_signal_plugin import MarkovPureSignalPlugin


PluginFactoryType = Callable[[StrategyPassport], BaseSignalPlugin]


class SignalPluginRegistry:
    """
    Регистрирует и создаёт сигнальные плагины по имени из паспорта ('signal.plugin').
    Позволяет подключать новые стратегии бесшовно через SignalPluginRegistry.register(...).
    """

    _REGISTRY: Dict[str, PluginFactoryType] = {
        "choch_4h": ChochSignalPlugin,
        "ema_cross": EmaCrossSignalPlugin,
        "bb_breakout": BollingerBreakoutSignalPlugin,
        "ema_pullback": EmaPullbackSignalPlugin,
        "macd_anti": MacdAntiSignalPlugin,
        "ict_silver_bullet": IctSilverBulletSignalPlugin,
        "gerchik_two_bar_fp": GerchikTwoBarFpSignalPlugin,
        "markov_pure": MarkovPureSignalPlugin,
    }

    @classmethod
    def register(cls, name: str, factory: PluginFactoryType) -> None:
        key = str(name).strip()
        if not key:
            raise ValueError("Signal plugin name must not be empty")
        cls._REGISTRY[key] = factory

    @classmethod
    def create(cls, passport: StrategyPassport) -> BaseSignalPlugin:
        key = passport.signal.plugin
        if key not in cls._REGISTRY:
            raise KeyError(f"Unknown signal plugin '{key}'. Registered: {sorted(cls._REGISTRY.keys())}")
        return cls._REGISTRY[key](passport)
