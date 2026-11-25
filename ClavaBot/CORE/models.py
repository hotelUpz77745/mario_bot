# Path: ClavaBot/CORE/models.py
# Role: Типизированные модели данных (свечи, свинги, сигналы, ордера, позиции, сделки)

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


SideType = Literal["LONG", "SHORT"]
RegimeType = Literal["NEUTRAL", "BULLISH", "BEARISH"]
SwingKindType = Literal["HH", "HL", "LH", "LL", "SH_INIT", "SL_INIT"]
ExitReasonType = Literal[
    "STOP_LOSS",
    "TAKE_PROFIT",
    "OPPOSITE_SIGNAL",
    "SIGNAL_EXIT",
    "BAILOUT_EXIT",
    "END_OF_SIM",
    "MANUAL",
]


@dataclass(frozen=True, slots=True)
class Candle:
    """Единая модель OHLCV-свечи любого таймфрейма."""
    symbol: str
    open_ts_ms: int
    close_ts_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class SwingPoint:
    """Подтверждённый локальный экстремум (свинг) на графике."""
    bar_index: int
    ts_ms: int
    price: float
    polarity: Literal["HIGH", "LOW"]
    kind: SwingKindType


@dataclass(frozen=True, slots=True)
class MarketStructureState:
    """Снимок рыночной структуры после закрытия очередной свечи."""
    symbol: str
    bar_index: int
    regime: RegimeType
    last_hh: Optional[SwingPoint]
    last_hl: Optional[SwingPoint]
    last_lh: Optional[SwingPoint]
    last_ll: Optional[SwingPoint]
    last_swing_high: Optional[SwingPoint]
    last_swing_low: Optional[SwingPoint]


@dataclass(frozen=True, slots=True)
class SignalEvent:
    """Универсальное событие входа/разворота от любого сигнального плагина."""
    symbol: str
    side: SideType
    signal_ts_ms: int
    entry_ref_price: float
    broken_level_price: Optional[float] = None
    broken_level_bar_index: Optional[int] = None
    origin_extreme_price: Optional[float] = None
    size_multiplier: float = 1.0


@dataclass(frozen=True, slots=True)
class OrderPlan:
    """Сформированный план рыночного входа с опциональными жёсткими SL/TP."""
    symbol: str
    side: SideType
    qty: float
    entry_ref_price: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    signal_ts_ms: int
    notional_usd: float


@dataclass(slots=True)
class OpenPosition:
    """Активная позиция в брокере/симуляторе."""
    symbol: str
    side: SideType
    qty: float
    entry_price: float
    entry_ts_ms: int
    stop_loss: Optional[float]
    take_profit: Optional[float]
    margin_used_usd: float
    entry_fee_usd: float


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """Завершённая сделка для журнала и блока аналитики."""
    symbol: str
    side: SideType
    qty: float
    entry_price: float
    exit_price: float
    entry_ts_ms: int
    exit_ts_ms: int
    stop_loss: Optional[float]
    take_profit: Optional[float]
    gross_pnl_usd: float
    fees_usd: float
    net_pnl_usd: float
    exit_reason: ExitReasonType
    margin_used_usd: float
