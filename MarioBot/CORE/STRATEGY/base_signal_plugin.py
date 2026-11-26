# Path: MarioBot/CORE/STRATEGY/base_signal_plugin.py
# Role: Абстрактный интерфейс инъектируемого сигнального плагина стратегии

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional
from MarioBot.CORE.models import Candle, ExitReasonType, OpenPosition, SignalEvent


class BaseSignalPlugin(ABC):
    """
    Универсальный контракт сигнального слоя для TradeEngineOrchestrator.
    Любая новая стратегия подключается путём реализации этого интерфейса и регистрации в SignalPluginRegistry.
    """

    @abstractmethod
    def update_and_evaluate(self, symbol: str, candle: Candle) -> Optional[SignalEvent]:
        """Обновляет внутреннее состояние на закрытой свече и возвращает SignalEvent либо None."""
        raise NotImplementedError

    def check_exit(self, symbol: str, candle: Candle, open_pos: OpenPosition) -> Optional[ExitReasonType]:
        """
        Опциональный хук для стратегий со скользящим или временным выходом (Trail / Bailout Exit).
        Возвращает причину закрытия позиции или None.
        """
        return None

    def check_scale_in(self, symbol: str, candle: Candle, open_pos: OpenPosition) -> Optional[float]:
        """
        Опциональный хук для добора пробной позиции после слома боковика.
        Возвращает целевой итоговый size_multiplier (например 1.0) или None.
        """
        return None
