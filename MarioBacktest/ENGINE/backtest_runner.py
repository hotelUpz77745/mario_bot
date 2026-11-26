# Path: MarioBacktest/ENGINE/backtest_runner.py
# Role: Высокоскоростной 1m-прогон через единый TradeEngineOrchestrator (4H-блоки + 1m SL/TP)

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import numpy as np
from MarioBot.ANALYTICS.equity_tracker import EquityTracker
from MarioBot.ANALYTICS.metrics_engine import AnalyticsMetricsEngine
from MarioBot.CORE.candle_aggregator import timeframe_to_ms
from MarioBot.CORE.config_loader import StrategyPassport
from MarioBot.CORE.models import Candle, OpenPosition
from MarioBot.CORE.orchestrator import TradeEngineOrchestrator
from MarioBot.CORE.spec_manager import SpecManager
from MarioBacktest.ENGINE.sim_broker import SimExecutionBroker
from MarioBacktest.ENGINE.vision_parser import Aligned1mDataset


def _aggregate_4h_blocks(
    ts_ms: np.ndarray,
    ohlcv: np.ndarray,
    valid: np.ndarray,
    tf_ms: int,
    symbols: List[str],
    active_indices: List[Tuple[str, int]],
) -> List[Tuple[int, int, Dict[str, Candle], np.ndarray]]:
    """
    Разбивает выровненную 1m матрицу на блоки старшего таймфрейма [b_start, b_end)
    и предвычисляет готовые Candle для каждого символа в блоке без потери 1m-индексов.
    """
    n = int(ts_ms.shape[0])
    if n == 0:
        return []

    bucket_ids = ts_ms // tf_ms
    change_pts = np.flatnonzero(bucket_ids[1:] != bucket_ids[:-1]) + 1
    starts = np.concatenate(([0], change_pts))
    ends = np.concatenate((change_pts, [n]))

    blocks: List[Tuple[int, int, Dict[str, Candle], np.ndarray]] = []
    for b_idx in range(len(starts)):
        s_i = int(starts[b_idx])
        e_i = int(ends[b_idx])
        b_open_ms = int(bucket_ids[s_i]) * tf_ms
        b_close_ms = b_open_ms + tf_ms
        block_candles: Dict[str, Candle] = {}

        for sym, sym_col in active_indices:
            v_slice = valid[s_i:e_i, sym_col]
            if not np.any(v_slice):
                continue
            sub = ohlcv[s_i:e_i, sym_col][v_slice]
            block_candles[sym] = Candle(
                symbol=sym,
                open_ts_ms=b_open_ms,
                close_ts_ms=b_close_ms,
                open=float(sub[0, 0]),
                high=float(np.max(sub[:, 1])),
                low=float(np.min(sub[:, 2])),
                close=float(sub[-1, 3]),
                volume=float(np.sum(sub[:, 4])),
            )
        blocks.append((s_i, e_i, block_candles, ohlcv[s_i:e_i]))
    return blocks


class BacktestRunner:
    """
    Исполняет стратегию по паспорту на выровненном 1-минутном наборе данных:
    - Прогревает оркестратор на 4H-свечах из 1m-истории (trading_enabled=False).
    - На боевом отрезке закрывает 4H-свечу, исполняет MARKET-входы на 1m Open
      и проверяет SL/TP на каждой 1m-свече (SL раньше TP).
    """

    def __init__(self, spec_manager: SpecManager, exec_cfg: Dict[str, Any]):
        self.spec_manager = spec_manager
        self.taker_fee_rate = float(exec_cfg["taker_fee_rate"])
        self.maker_fee_rate = float(exec_cfg["maker_fee_rate"])
        self.slippage_rate = float(exec_cfg["slippage_rate"])
        self.tp_slippage_rate = float(exec_cfg["tp_slippage_rate"])
        self.annualization_days = int(exec_cfg["annualization_days"])
        self.equity_sample_interval_minutes = int(exec_cfg["equity_sample_interval_minutes"])
        self._blocks_cache: Dict[Tuple[int, int, int, Tuple[str, ...]], Tuple[Any, Any]] = {}

    @staticmethod
    def _pos_triggered_in_block(pos: OpenPosition, bar: Optional[Candle]) -> bool:
        if bar is None:
            return True
        if pos.side == "LONG":
            if pos.stop_loss is not None and bar.low <= pos.stop_loss:
                return True
            if pos.take_profit is not None and bar.high >= pos.take_profit:
                return True
            return False
        if pos.stop_loss is not None and bar.high >= pos.stop_loss:
            return True
        if pos.take_profit is not None and bar.low <= pos.take_profit:
            return True
        return False

    def _get_blocks(
        self,
        dataset: Aligned1mDataset,
        tf_ms: int,
        active_indices: List[Tuple[str, int]],
    ) -> Tuple[Any, Any]:
        cache_key = (id(dataset), int(dataset.sim_ts_ms.shape[0]), tf_ms, tuple(s for s, _ in active_indices))
        cached = self._blocks_cache.get(cache_key)
        if cached is not None:
            return cached
        warmup_blocks = _aggregate_4h_blocks(
            dataset.warmup_ts_ms,
            dataset.warmup_ohlcv,
            dataset.warmup_valid,
            tf_ms,
            dataset.symbols,
            active_indices,
        )
        sim_blocks = _aggregate_4h_blocks(
            dataset.sim_ts_ms,
            dataset.sim_ohlcv,
            dataset.sim_valid,
            tf_ms,
            dataset.symbols,
            active_indices,
        )
        if len(self._blocks_cache) >= 8:
            self._blocks_cache.clear()
        self._blocks_cache[cache_key] = (warmup_blocks, sim_blocks)
        return warmup_blocks, sim_blocks

    def run_passport(
        self,
        passport: StrategyPassport,
        dataset: Aligned1mDataset,
    ) -> Dict[str, Any]:
        broker = SimExecutionBroker(
            start_balance_usd=passport.capital.start_balance_usd,
            leverage=passport.capital.leverage,
            taker_fee_rate=self.taker_fee_rate,
            maker_fee_rate=self.maker_fee_rate,
            slippage_rate=self.slippage_rate,
            tp_slippage_rate=self.tp_slippage_rate,
        )
        orchestrator = TradeEngineOrchestrator(
            passport=passport,
            spec_manager=self.spec_manager,
            broker=broker,
        )
        sig_params = passport.signal.params
        intrabar_trail = (
            str(sig_params.get("profit_trail_mode", "4h_close")) == "intrabar_tick"
            and sig_params.get("profit_trail_activation_atr_mult") is not None
            and sig_params.get("profit_trail_distance_atr_mult") is not None
        )
        if intrabar_trail:
            broker.configure_intrabar_profit_trail(
                float(sig_params["profit_trail_activation_atr_mult"]),
                float(sig_params["profit_trail_distance_atr_mult"]),
            )
        tracker = EquityTracker(passport.capital.start_balance_usd)
        tf_ms = timeframe_to_ms(passport.timeframe)

        sym_to_idx = {s: idx for idx, s in enumerate(dataset.symbols)}
        active_indices = [(sym, sym_to_idx[sym]) for sym in passport.symbols if sym in sym_to_idx]
        bench_sym = passport.market_sentinel.benchmark_symbol
        feed_indices = list(active_indices)
        if bench_sym is not None and bench_sym in sym_to_idx and bench_sym not in passport.symbols:
            feed_indices.append((bench_sym, sym_to_idx[bench_sym]))

        # 1. Прогрев на блоках таймфрейма
        orchestrator.set_trading_enabled(False)
        warmup_blocks, sim_blocks = self._get_blocks(dataset, tf_ms, feed_indices)
        for _s, _e, block_candles, _slice in warmup_blocks:
            if bench_sym is not None and bench_sym in block_candles:
                orchestrator.on_benchmark_candle(block_candles[bench_sym])
            for sym, _col in active_indices:
                bar = block_candles.get(sym)
                if bar is not None:
                    orchestrator.on_candle_close(sym, bar)
        if intrabar_trail and hasattr(orchestrator.signal_plugin, "get_atr"):
            for sym, _col in active_indices:
                broker.update_symbol_atr(sym, orchestrator.signal_plugin.get_atr(sym))  # type: ignore[attr-defined]

        # 2. Боевая симуляция: решения по закрытию свечи ТФ + точная 1m проверка триггеров SL/TP
        orchestrator.set_trading_enabled(True)

        s_ts = dataset.sim_ts_ms
        s_valid = dataset.sim_valid
        s_fund_rates = dataset.sim_funding_rates
        s_fund_mask = dataset.sim_funding_mask
        t_len = int(s_ts.shape[0])
        prev_candles: Optional[Dict[str, Candle]] = None

        # Единая прекомпиляция событий фандинга на индексы блоков (O(1) в цикле симуляции)
        block_funding_events: List[List[Tuple[str, int, float]]] = []
        if s_fund_rates is not None and s_fund_mask is not None:
            for s_i, e_i, _bc, _bo in sim_blocks:
                ev: List[Tuple[str, int, float]] = []
                if np.any(s_fund_mask[s_i:e_i]):
                    f_local = np.flatnonzero(s_fund_mask[s_i:e_i])
                    for off in f_local.tolist():
                        abs_idx = s_i + off
                        ts_ev = int(s_ts[abs_idx])
                        for sym, col in active_indices:
                            rt = float(s_fund_rates[abs_idx, col])
                            if rt != 0.0:
                                ev.append((sym, ts_ev, rt))
                block_funding_events.append(ev)

        for blk_idx, (s_i, e_i, block_candles, block_ohlcv) in enumerate(sim_blocks):
            first_ts_ms = int(s_ts[s_i])
            if block_funding_events:
                for sym_f, ts_f, rt_f in block_funding_events[blk_idx]:
                    orchestrator.on_funding_rate(sym_f, rt_f)
                    if sym_f in broker.positions:
                        bar_f = block_candles.get(sym_f)
                        mark_px = (
                            bar_f.open
                            if bar_f is not None
                            else broker.last_close_prices.get(sym_f, broker.positions[sym_f].entry_price)
                        )
                        broker.apply_funding(sym_f, ts_f, rt_f, mark_px)
            if prev_candles is not None:
                if bench_sym is not None and bench_sym in prev_candles:
                    orchestrator.on_benchmark_candle(prev_candles[bench_sym])
                for sym, _col in active_indices:
                    prev_bar = prev_candles.get(sym)
                    if prev_bar is not None:
                        orchestrator.on_candle_close(sym, prev_bar)
                        if intrabar_trail and hasattr(orchestrator.signal_plugin, "get_atr"):
                            broker.update_symbol_atr(sym, orchestrator.signal_plugin.get_atr(sym))  # type: ignore[attr-defined]

            # Исполняем отложенные закрытия, MARKET-входы и доборы по Open первой доступной 1m свечи блока
            if broker.pending_orders or broker.pending_closes or broker.pending_scale_ins:
                for sym, col in active_indices:
                    if (
                        sym not in broker.pending_orders
                        and sym not in broker.pending_closes
                        and sym not in broker.pending_scale_ins
                    ):
                        continue
                    v_col = s_valid[s_i:e_i, col]
                    if np.any(v_col):
                        first_pos = int(np.flatnonzero(v_col)[0])
                        open_px = float(block_ohlcv[first_pos, col, 0])
                        open_ts = int(s_ts[s_i + first_pos])
                        broker.fill_pending_at_minute_open(sym, open_ts, open_px)

            # Коридор-фильтр: 1m-перебор запускается ТОЛЬКО если внутри блока задет SL/TP или активен 1m профит-трейлинг
            if broker.positions:
                triggered_cols = [
                    (sym, col)
                    for sym, col in active_indices
                    if sym in broker.positions
                    and (
                        self._pos_triggered_in_block(broker.positions[sym], block_candles.get(sym))
                        or (
                            intrabar_trail
                            and block_candles.get(sym) is not None
                            and broker.is_intrabar_trail_relevant(
                                broker.positions[sym],
                                block_candles[sym].high,
                                block_candles[sym].low,
                            )
                        )
                    )
                ]
                if triggered_cols:
                    block_len = e_i - s_i
                    for k in range(block_len):
                        t_ms = int(s_ts[s_i + k])
                        for sym, col in triggered_cols:
                            if sym not in broker.positions:
                                continue
                            if not bool(s_valid[s_i + k, col]):
                                continue
                            row = block_ohlcv[k, col]
                            broker.process_1m_bar(
                                sym,
                                t_ms,
                                float(row[0]),
                                float(row[1]),
                                float(row[2]),
                                float(row[3]),
                            )
                        if not any(sym in broker.positions for sym, _c in triggered_cols):
                            break

                # Учёт внутриблочной просадки по худшему экстремуму выживших позиций (O(1))
                if broker.positions:
                    for sym, pos in broker.positions.items():
                        bar = block_candles.get(sym)
                        if bar is not None:
                            broker.last_close_prices[sym] = bar.low if pos.side == "LONG" else bar.high
                    tracker.record(int(s_ts[e_i - 1]), broker.get_equity_usd(), record_curve_point=False)

            for sym, _col in active_indices:
                bar = block_candles.get(sym)
                if bar is not None:
                    broker.last_close_prices[sym] = bar.close

            end_block_ts = int(s_ts[e_i - 1])
            tracker.record(end_block_ts, broker.get_equity_usd(), record_curve_point=True)
            prev_candles = block_candles

        # Закрываем последний 4H-бар конца периода для симметрии
        if prev_candles is not None and t_len > 0:
            if bench_sym is not None and bench_sym in prev_candles:
                orchestrator.on_benchmark_candle(prev_candles[bench_sym])
            for sym, _col in active_indices:
                prev_bar = prev_candles.get(sym)
                if prev_bar is not None:
                    orchestrator.on_candle_close(sym, prev_bar)

        sim_start_ms = int(s_ts[0]) if t_len > 0 else 0
        sim_end_ms = int(s_ts[-1]) + 60_000 if t_len > 0 else 0
        broker.force_close_all(sim_end_ms)
        end_equity = broker.get_equity_usd()
        tracker.record(sim_end_ms, end_equity, record_curve_point=True)

        summary = AnalyticsMetricsEngine.compute(
            strategy_id=passport.strategy_id,
            start_balance_usd=passport.capital.start_balance_usd,
            end_equity_usd=end_equity,
            trades=broker.closed_trades,
            tracker=tracker,
            sim_start_ms=sim_start_ms,
            sim_end_ms=sim_end_ms,
            annualization_days=self.annualization_days,
        )
        summary["exit_reasons"] = self._count_exit_reasons(broker)
        summary["total_funding_usd"] = round(broker.total_funding_usd, 2)
        return summary

    @staticmethod
    def _count_exit_reasons(broker: SimExecutionBroker) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for tr in broker.closed_trades:
            counts[tr.exit_reason] = counts.get(tr.exit_reason, 0) + 1
        return counts
