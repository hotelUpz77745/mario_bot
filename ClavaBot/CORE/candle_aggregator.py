# Path: ClavaBot/CORE/candle_aggregator.py
# Role: Детерминированный агрегатор 1m свечей в целевой таймфрейм (4H) по границам UTC

from __future__ import annotations

from typing import Dict, Optional
from ClavaBot.CORE.models import Candle


_TF_SECONDS_MAP: Dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


def timeframe_to_ms(timeframe: str) -> int:
    tf = timeframe.lower().strip()
    if tf not in _TF_SECONDS_MAP:
        raise ValueError(f"Unsupported timeframe '{timeframe}'. Supported: {list(_TF_SECONDS_MAP.keys())}")
    return _TF_SECONDS_MAP[tf] * 1000


class _ActiveBucket:
    __slots__ = ("open_ts_ms", "close_ts_ms", "open", "high", "low", "close", "volume")

    def __init__(self, open_ts_ms: int, close_ts_ms: int, o: float, h: float, l: float, c: float, v: float):
        self.open_ts_ms = open_ts_ms
        self.close_ts_ms = close_ts_ms
        self.open = o
        self.high = h
        self.low = l
        self.close = c
        self.volume = v


class CandleAggregator:
    """
    Собирает 1m свечи в старшие свечи (4H) с нулевым заглядыванием в будущее.
    Возвращает закрытую 4H-свечу либо когда приходит 1m свеча следующего бакета (flush),
    либо в момент достижения последней минуты текущего бакета.
    """

    def __init__(self, timeframe: str):
        self.timeframe = timeframe.lower().strip()
        self.tf_ms: int = timeframe_to_ms(self.timeframe)
        self._buckets: Dict[str, _ActiveBucket] = {}

    def flush_if_closed(self, symbol: str, current_1m_open_ts_ms: int) -> Optional[Candle]:
        """
        Если текущая 1m свеча уже относится к следующему бакету (>= active.close_ts_ms),
        закрывает и возвращает завершённый предыдущий 4H-бар ДО обработки новой минуты.
        """
        bucket = self._buckets.get(symbol)
        if bucket is None:
            return None
        if current_1m_open_ts_ms >= bucket.close_ts_ms:
            closed = Candle(
                symbol=symbol,
                open_ts_ms=bucket.open_ts_ms,
                close_ts_ms=bucket.close_ts_ms,
                open=bucket.open,
                high=bucket.high,
                low=bucket.low,
                close=bucket.close,
                volume=bucket.volume,
            )
            del self._buckets[symbol]
            return closed
        return None

    def push_1m(
        self,
        symbol: str,
        open_ts_ms: int,
        o: float,
        h: float,
        l: float,
        c: float,
        v: float,
    ) -> None:
        """Обновляет текущий активный 4H-бакет данными 1m-свечи."""
        bucket_open = (open_ts_ms // self.tf_ms) * self.tf_ms
        bucket_close = bucket_open + self.tf_ms

        existing = self._buckets.get(symbol)
        if existing is None or existing.open_ts_ms != bucket_open:
            self._buckets[symbol] = _ActiveBucket(
                open_ts_ms=bucket_open,
                close_ts_ms=bucket_close,
                o=o,
                h=h,
                l=l,
                c=c,
                v=v,
            )
            return

        if h > existing.high:
            existing.high = h
        if l < existing.low:
            existing.low = l
        existing.close = c
        existing.volume += v
