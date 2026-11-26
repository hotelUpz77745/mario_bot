# Path: MarioBot/CORE/orchestrator.py
# Role: Единый универсальный оркестратор жизненного цикла с инъекцией сигнального плагина

from __future__ import annotations

from typing import List, Optional
from MarioBot.CORE.config_loader import StrategyPassport
from MarioBot.CORE.EXECUTION.base_broker import ExecutionBroker
from MarioBot.CORE.models import Candle, OpenPosition, OrderPlan
from MarioBot.CORE.spec_manager import SpecManager
from MarioBot.CORE.STRATEGY.base_signal_plugin import BaseSignalPlugin
from MarioBot.CORE.STRATEGY.ema_trend_filter import EmaTrendFilter
from MarioBot.CORE.STRATEGY.grid_remora_gate import GridRemoraGate, GridStateProvider
from MarioBot.CORE.STRATEGY.markov_regime_filter import MarkovRegimeFilter
from MarioBot.CORE.STRATEGY.market_sentinel import MarketSentinel
from MarioBot.CORE.STRATEGY.portfolio_risk_guard import PortfolioRiskGuard
from MarioBot.CORE.STRATEGY.position_sizer import PositionSizer
from MarioBot.CORE.STRATEGY.remora_dynamic_sizer import RemoraDynamicSizer
from MarioBot.CORE.STRATEGY.risk_sl_tp_calculator import RiskStopTargetCalculator
from MarioBot.CORE.STRATEGY.session_window_filter import SessionWindowFilter
from MarioBot.CORE.STRATEGY.signal_registry import SignalPluginRegistry
from MarioBot.CORE.STRATEGY.trend_fatigue_filter import TrendFatigueFilter


class TradeEngineOrchestrator:
    """
    Универсальный жизненный цикл любой стратегии (Prod и Backtest):
    1. Обновляет инъектируемый signal_plugin, TrendFatigueFilter и MarkovRegimeFilter.
    2. Если открыта противоположная позиция и включён exit_on_opposite_signal,
       закрывает её и использует тот же сигнал для разворота (с проверкой фильтров входа).
    """

    def __init__(
        self,
        passport: StrategyPassport,
        spec_manager: SpecManager,
        broker: ExecutionBroker,
        signal_plugin: Optional[BaseSignalPlugin] = None,
        grid_state_provider: Optional[GridStateProvider] = None,
    ):
        self.passport = passport
        self.spec_manager = spec_manager
        self.broker = broker
        self.trading_enabled: bool = True

        self.signal_plugin: BaseSignalPlugin = (
            signal_plugin if signal_plugin is not None else SignalPluginRegistry.create(passport)
        )
        self.session_filter = SessionWindowFilter(passport.session_window)
        self.fatigue_filter = TrendFatigueFilter(passport.trend_fatigue_filter, passport.timeframe)
        self.ema_trend_filter = EmaTrendFilter(passport.ema_trend_filter)
        self.markov_filter = MarkovRegimeFilter(passport.markov_regime)
        self.market_sentinel = MarketSentinel(passport.market_sentinel, passport.symbols)
        self.portfolio_risk = PortfolioRiskGuard(passport.portfolio_risk, passport.capital.start_balance_usd)
        self.risk_calculator = RiskStopTargetCalculator(passport.risk_management, spec_manager)
        self.position_sizer = PositionSizer(passport.capital, spec_manager)
        self.remora_gate = GridRemoraGate(passport.remora_gate, grid_state_provider)
        self.remora_sizer = RemoraDynamicSizer(
            leverage=passport.capital.leverage,
            cfg=passport.remora_gate.dynamic_sizing,
            spec_manager=spec_manager,
        )

    def set_grid_state_provider(self, provider: Optional[GridStateProvider]) -> None:
        self.remora_gate.set_provider(provider)

    def get_tracking_symbols(self, now_ms: int, universe_symbols: Optional[List[str]] = None) -> List[str]:
        """Возвращает активный пул HRON_3 + монеты, по которым в Mario ещё открыты позиции."""
        candidates = universe_symbols if universe_symbols is not None else self.passport.symbols
        if not self.remora_gate.cfg.enabled:
            return list(candidates)
        tracked = set(self.remora_gate.get_active_pool_symbols(now_ms))
        for sym in candidates:
            if self.broker.has_open_position(sym):
                tracked.add(sym)
        return sorted(tracked)

    def set_trading_enabled(self, enabled: bool) -> None:
        self.trading_enabled = enabled

    def on_benchmark_candle(self, candle: Candle) -> None:
        self.market_sentinel.update_benchmark(candle)

    def on_funding_rate(self, symbol: str, funding_rate: float) -> None:
        self.portfolio_risk.update_funding_rate(symbol, funding_rate)

    def on_candle_close(self, symbol: str, candle: Candle) -> Optional[OrderPlan]:
        signal = self.signal_plugin.update_and_evaluate(symbol, candle)
        self.fatigue_filter.update(symbol, candle)
        self.ema_trend_filter.update(symbol, candle)
        self.markov_filter.update(symbol, candle)
        mk_state = (
            self.markov_filter.evaluate(symbol, "LONG").current_state
            if self.passport.markov_regime.enabled
            else "DISABLED"
        )
        self.market_sentinel.update_symbol(symbol, candle, mk_state)

        if not self.trading_enabled:
            return None

        self.portfolio_risk.update_equity(self.broker.get_equity_usd())

        open_pos = self.broker.get_open_position(symbol)
        if open_pos is not None:
            exit_reason = self.signal_plugin.check_exit(symbol, candle, open_pos)
            if exit_reason is not None:
                self.broker.submit_close_market(symbol, reason=exit_reason)
                return None
            if signal is None or signal.side == open_pos.side:
                target_mult = self.signal_plugin.check_scale_in(symbol, candle, open_pos)
                if target_mult is not None and target_mult > 0.0:
                    self._try_scale_in(symbol, candle, open_pos, target_mult)

        if signal is None:
            return None

        open_side = self.broker.get_open_position_side(symbol)
        is_reversal = False
        if open_side is not None:
            if not self.passport.signal.exit_on_opposite_signal or open_side == signal.side:
                return None
            self.broker.submit_close_market(symbol, reason="OPPOSITE_SIGNAL")
            is_reversal = True

        max_pos = self.passport.capital.max_open_positions
        effective_open = self.broker.open_positions_count() - (1 if is_reversal else 0)
        if max_pos is not None and effective_open >= max_pos:
            return None

        if not self.session_filter.is_allowed(candle.close_ts_ms):
            return None

        if self.fatigue_filter.is_fatigued(symbol, signal.side):
            return None

        if not self.ema_trend_filter.is_allowed(symbol, signal.side):
            return None

        markov_decision = self.markov_filter.evaluate(symbol, signal.side)
        if not markov_decision.allowed:
            return None

        sentinel_decision = self.market_sentinel.evaluate(signal.side)
        if not sentinel_decision.allowed:
            return None

        pr_decision = self.portfolio_risk.evaluate(
            symbol=symbol,
            side=signal.side,
            equity_usd=self.broker.get_equity_usd(),
            open_positions=self.broker.get_all_open_positions(),
            exclude_symbol=symbol if is_reversal else None,
        )
        if not pr_decision.allowed:
            return None

        if self.remora_gate.cfg.enabled:
            if is_reversal and not self.remora_gate.cfg.allow_reversal_outside_pool:
                if not self.remora_gate.is_in_active_pool(symbol, candle.close_ts_ms):
                    return None
            if not self.remora_gate.can_open_entry(symbol, signal.side, candle.close_ts_ms):
                return None

        is_valid, sl_price, tp_price = self.risk_calculator.calculate(signal)
        if not is_valid:
            return None

        total_mult = (
            signal.size_multiplier
            * markov_decision.size_multiplier
            * sentinel_decision.size_multiplier
            * pr_decision.size_multiplier
        )
        if self.remora_gate.cfg.enabled and self.remora_gate.cfg.dynamic_sizing.enabled:
            qty = self.remora_sizer.calculate_qty(
                symbol=symbol,
                equity_usd=self.broker.get_equity_usd(),
                free_margin_usd=self.broker.get_free_margin_usd(),
                entry_price=signal.entry_ref_price,
                open_positions_count=effective_open,
                triggered_pool_count=self.remora_gate.count_triggered_coins(candle.close_ts_ms),
                size_multiplier=total_mult,
            )
        else:
            qty = self.position_sizer.calculate_qty(
                symbol=symbol,
                equity_usd=self.broker.get_equity_usd(),
                free_margin_usd=self.broker.get_free_margin_usd(),
                entry_price=signal.entry_ref_price,
                stop_loss_price=sl_price,
                size_multiplier=total_mult,
            )
        if qty <= 0.0:
            return None

        plan = OrderPlan(
            symbol=symbol,
            side=signal.side,
            qty=qty,
            entry_ref_price=signal.entry_ref_price,
            stop_loss=sl_price,
            take_profit=tp_price,
            signal_ts_ms=signal.signal_ts_ms,
            notional_usd=qty * signal.entry_ref_price,
        )
        submitted = self.broker.submit_market_with_bracket(plan)
        return plan if submitted else None

    def _try_scale_in(self, symbol: str, candle: Candle, open_pos: OpenPosition, target_mult: float) -> None:
        markov_decision = self.markov_filter.evaluate(symbol, open_pos.side)
        if not markov_decision.allowed:
            return
        if self.remora_gate.cfg.enabled and self.remora_gate.cfg.dynamic_sizing.enabled:
            target_qty = self.remora_sizer.calculate_qty(
                symbol=symbol,
                equity_usd=self.broker.get_equity_usd(),
                free_margin_usd=self.broker.get_free_margin_usd() + open_pos.margin_used_usd,
                entry_price=candle.close,
                open_positions_count=max(0, self.broker.open_positions_count() - 1),
                triggered_pool_count=self.remora_gate.count_triggered_coins(candle.close_ts_ms),
                size_multiplier=target_mult * markov_decision.size_multiplier,
            )
        else:
            target_qty = self.position_sizer.calculate_qty(
                symbol=symbol,
                equity_usd=self.broker.get_equity_usd(),
                free_margin_usd=self.broker.get_free_margin_usd(),
                entry_price=candle.close,
                stop_loss_price=open_pos.stop_loss,
                size_multiplier=target_mult * markov_decision.size_multiplier,
            )
        add_qty = self.spec_manager.round_qty(symbol, max(0.0, target_qty - open_pos.qty))
        if add_qty <= 0.0 or not self.spec_manager.check_min_notional(symbol, add_qty, candle.close):
            return
        plan = OrderPlan(
            symbol=symbol,
            side=open_pos.side,
            qty=add_qty,
            entry_ref_price=candle.close,
            stop_loss=open_pos.stop_loss,
            take_profit=open_pos.take_profit,
            signal_ts_ms=candle.close_ts_ms,
            notional_usd=add_qty * candle.close,
        )
        self.broker.submit_scale_in_market(plan)
