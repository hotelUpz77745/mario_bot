# Path: ClavaBot/CORE/STRATEGY/market_sentinel.py
# Role: Кросс-рыночный фильтр (Market Breadth по портфелю + сентимент BTCUSDT как индикатора)

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List
from ClavaBot.CORE.config_loader import MarketSentinelConfig
from ClavaBot.CORE.models import Candle, SideType
from ClavaBot.CORE.STRATEGY._indicators import IncrementalADX, IncrementalEMA


@dataclass(frozen=True, slots=True)
class MarketSentinelDecision:
    allowed: bool
    size_multiplier: float
    btc_regime: str
    trending_coins: int


class MarketSentinel:
    """
    Отслеживает обще-рыночный контекст для снижения синхронных просадок в боковике:
    1. BTCUSDT Sentiment (чистый индикатор, без торговли BTC): EMA + ADX + N-дневная доходность.
    2. Market Breadth: доля монет портфеля в тренде (ADX >= min_adx и Марков != SIDEWAYS).
    """

    def __init__(self, cfg: MarketSentinelConfig, symbols: List[str]):
        self.cfg = cfg
        self._btc_ema = IncrementalEMA(cfg.btc_ema_period)
        self._btc_adx = IncrementalADX(cfg.btc_adx_period)
        self._btc_daily_closes: List[float] = []
        self._btc_last_day: int = -1
        self._btc_day_close: float = 0.0
        self.btc_regime: str = "WARMING"

        self._sym_adxs: Dict[str, IncrementalADX] = {
            sym: IncrementalADX(cfg.breadth_adx_period) for sym in symbols
        }
        self._coin_trending: Dict[str, bool] = {sym: True for sym in symbols}

    def update_benchmark(self, candle: Candle) -> None:
        if not self.cfg.enabled or not self.cfg.btc_sentiment_enabled:
            return
        ema_val = self._btc_ema.update(candle.close)
        adx_val = self._btc_adx.update(candle)

        day_idx = (candle.close_ts_ms - 1) // 86_400_000
        if self._btc_last_day < 0:
            self._btc_last_day = day_idx
            self._btc_day_close = candle.close
        elif day_idx > self._btc_last_day:
            self._btc_daily_closes.append(self._btc_day_close)
            self._btc_last_day = day_idx
            self._btc_day_close = candle.close
        else:
            self._btc_day_close = candle.close

        req = self.cfg.btc_ret_lookback_days + 1
        if ema_val is None or adx_val is None or len(self._btc_daily_closes) < req:
            self.btc_regime = "WARMING"
            return

        prev_px = self._btc_daily_closes[-req]
        ret = ((self._btc_daily_closes[-1] - prev_px) / prev_px) if prev_px > 0.0 else 0.0

        if adx_val < self.cfg.btc_chop_adx_threshold:
            self.btc_regime = "CHOP"
        elif candle.close > ema_val and ret > -0.02:
            self.btc_regime = "BULL"
        elif candle.close < ema_val and ret < 0.02:
            self.btc_regime = "BEAR"
        else:
            self.btc_regime = "CHOP"

    def update_symbol(self, symbol: str, candle: Candle, markov_state: str) -> None:
        if not self.cfg.enabled or not self.cfg.breadth_enabled:
            return
        adx_tracker = self._sym_adxs.get(symbol)
        if adx_tracker is None:
            adx_tracker = IncrementalADX(self.cfg.breadth_adx_period)
            self._sym_adxs[symbol] = adx_tracker
        ax = adx_tracker.update(candle)
        self._coin_trending[symbol] = (
            (ax is None or ax >= self.cfg.breadth_min_adx) and (markov_state != "SIDEWAYS")
        )

    def evaluate(self, side: SideType) -> MarketSentinelDecision:
        trending_cnt = sum(1 for v in self._coin_trending.values() if v)
        if not self.cfg.enabled:
            return MarketSentinelDecision(True, 1.0, "DISABLED", trending_cnt)

        mult = 1.0
        if self.cfg.btc_sentiment_enabled and self.btc_regime != "WARMING":
            if self.cfg.block_long_in_btc_bear and side == "LONG" and self.btc_regime == "BEAR":
                return MarketSentinelDecision(False, 0.0, self.btc_regime, trending_cnt)
            if self.btc_regime == "CHOP":
                mult *= self.cfg.btc_chop_size_multiplier
            elif side == "LONG" and self.btc_regime == "BEAR":
                mult *= self.cfg.btc_counter_long_multiplier
            elif side == "SHORT" and self.btc_regime == "BULL":
                mult *= self.cfg.btc_counter_short_multiplier

        if self.cfg.breadth_enabled and trending_cnt < self.cfg.breadth_min_trending_coins:
            mult *= self.cfg.breadth_defensive_multiplier

        return MarketSentinelDecision(True, mult, self.btc_regime, trending_cnt)
