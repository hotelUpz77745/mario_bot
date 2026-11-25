# Path: ClavaBot/CORE/EXECUTION/base_broker.py
# Role: Абстрактный интерфейс брокера исполнения (единый контракт для Prod и Backtest)

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional
from ClavaBot.CORE.models import OpenPosition, OrderPlan, SideType


class ExecutionBroker(ABC):
    """Абстрактный брокер: скрывает различия между боевым API, мок-брокером и 1m-симулятором."""

    @abstractmethod
    def has_open_position(self, symbol: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get_open_position(self, symbol: str) -> Optional[OpenPosition]:
        raise NotImplementedError

    @abstractmethod
    def get_open_position_side(self, symbol: str) -> Optional[SideType]:
        raise NotImplementedError

    @abstractmethod
    def open_positions_count(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def get_equity_usd(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def get_free_margin_usd(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def submit_close_market(self, symbol: str, reason: str = "OPPOSITE_SIGNAL") -> bool:
        raise NotImplementedError

    @abstractmethod
    def submit_market_with_bracket(self, plan: OrderPlan) -> bool:
        raise NotImplementedError

    def submit_scale_in_market(self, plan: OrderPlan) -> bool:
        """Опциональный добор активной позиции по тренду (по умолчанию отключён для простых моков)."""
        return False
