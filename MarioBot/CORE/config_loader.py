# Path: MarioBot/CORE/config_loader.py
# Role: Строгий загрузчик и валидатор конфигураций (правило прямого доступа ['key'])

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True, slots=True)
class CapitalConfig:
    start_balance_usd: float
    leverage: int
    max_open_positions: Optional[int]
    risk_per_trade_ratio: Optional[float]
    fixed_notional_usd: Optional[float]
    sizing_mode: str = "legacy_risk"
    reinvest_notional_equity_ratio: Optional[float] = None
    reinvest_apply_signal_multipliers: bool = True
    fixed_apply_signal_multipliers: bool = True


@dataclass(frozen=True, slots=True)
class SignalPluginConfig:
    plugin: str
    exit_on_opposite_signal: bool
    params: Dict[str, Any]


@dataclass(frozen=True, slots=True)
class SwingDetectorConfig:
    left_candles: int
    right_candles: int
    min_swing_sequence: int


@dataclass(frozen=True, slots=True)
class ChochDeterminatorConfig:
    breakout_price_source: str
    require_sweep: bool
    entry_order_type: str


@dataclass(frozen=True, slots=True)
class SessionWindowConfig:
    enabled: bool
    timezone: str
    start_time: str
    end_time: str
    weekdays_only: bool


@dataclass(frozen=True, slots=True)
class TrendFatigueConfig:
    enabled: bool
    lookback_days: int
    min_net_efficiency_ratio: float
    max_counter_pullback_ratio: float
    block_mode: str


@dataclass(frozen=True, slots=True)
class EmaTrendFilterConfig:
    enabled: bool
    period: int


@dataclass(frozen=True, slots=True)
class MarketSentinelConfig:
    enabled: bool
    benchmark_symbol: Optional[str]
    btc_sentiment_enabled: bool
    btc_ema_period: int
    btc_adx_period: int
    btc_chop_adx_threshold: float
    btc_ret_lookback_days: int
    btc_chop_size_multiplier: float
    btc_counter_long_multiplier: float
    btc_counter_short_multiplier: float
    block_long_in_btc_bear: bool
    breadth_enabled: bool
    breadth_adx_period: int
    breadth_min_adx: float
    breadth_min_trending_coins: int
    breadth_defensive_multiplier: float


@dataclass(frozen=True, slots=True)
class RemoraDynamicSizingConfig:
    enabled: bool
    min_slots: int
    max_slots: int
    min_slot_equity_pct: float
    max_slot_equity_pct: float
    equity_utilization: float
    margin_safety_ratio: float


@dataclass(frozen=True, slots=True)
class RemoraGateConfig:
    enabled: bool
    mode: str
    min_grid_level: int
    min_adv_pct: Optional[float]
    allow_reversal_outside_pool: bool
    snapshot_path: Optional[str]
    max_staleness_ms: int
    dynamic_sizing: RemoraDynamicSizingConfig


@dataclass(frozen=True, slots=True)
class RiskManagementConfig:
    sl_anchor: str
    sl_buffer_ratio: Optional[float]
    tp_rr_ratio: Optional[float]
    soft_stop_enabled: bool


@dataclass(frozen=True, slots=True)
class MarkovRegimeConfig:
    enabled: bool
    return_lookback_days: int
    bull_return_threshold_ratio: float
    bear_return_threshold_ratio: float
    transition_window_days: Optional[int]
    min_history_states: int
    forecast_horizon_days: int
    laplace_alpha: float
    block_long_in_bear: bool
    block_short_in_bull: bool
    min_net_score_for_long_ratio: Optional[float]
    min_net_score_for_short_ratio: Optional[float]
    dynamic_sizing_enabled: bool
    high_persistence_threshold_ratio: float
    aligned_high_persistence_multiplier: float
    sideways_size_multiplier: float
    counter_regime_size_multiplier: float


@dataclass(frozen=True, slots=True)
class PortfolioRiskConfig:
    enabled: bool
    max_same_side_positions: Optional[int]
    max_portfolio_risk_ratio: Optional[float]
    dd_throttle_enabled: bool
    dd_tier1_ratio: float
    dd_tier1_multiplier: float
    dd_tier2_ratio: float
    dd_tier2_multiplier: float
    funding_guard_enabled: bool
    max_adverse_funding_rate: float
    adverse_funding_multiplier: float


@dataclass(frozen=True, slots=True)
class StrategyPassport:
    """Типизированный и провалидированный паспорт универсальной стратегии."""
    strategy_id: str
    enabled: bool
    symbols: List[str]
    timeframe: str
    capital: CapitalConfig
    signal: SignalPluginConfig
    session_window: SessionWindowConfig
    trend_fatigue_filter: TrendFatigueConfig
    ema_trend_filter: EmaTrendFilterConfig
    risk_management: RiskManagementConfig
    markov_regime: MarkovRegimeConfig
    market_sentinel: MarketSentinelConfig
    remora_gate: RemoraGateConfig
    portfolio_risk: PortfolioRiskConfig


class StrictConfigLoader:
    """Загружает JSON-конфигурации строго через ['key'] без .get(..., default)."""

    @staticmethod
    def read_json(path: Path) -> Dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError(f"Expected JSON object in {path}")
        return data

    @classmethod
    def load_strategy_passport(cls, path: Path) -> StrategyPassport:
        raw = cls.read_json(path)
        return cls.parse_strategy_passport(raw)

    @staticmethod
    def parse_strategy_passport(raw: Dict[str, Any]) -> StrategyPassport:
        cap_raw = raw["capital"]
        sig_raw = raw["signal"]
        ses_raw = raw["session_window"]
        fat_raw = raw["trend_fatigue_filter"]
        et_raw = raw["ema_trend_filter"]
        rm_raw = raw["risk_management"]
        mk_raw = raw["markov_regime"]
        ms_raw = raw["market_sentinel"]
        rg_raw = raw["remora_gate"]
        rg_ds_raw = rg_raw["dynamic_sizing"]
        pr_raw = raw["portfolio_risk"]

        max_pos = cap_raw["max_open_positions"]
        if "sizing" in cap_raw:
            sz_raw = cap_raw["sizing"]
            sz_mode = str(sz_raw["mode"]).strip().lower()
            if sz_mode not in ("reinvest", "fixed"):
                raise ValueError(f"Unsupported capital.sizing.mode: {sz_mode} (expected 'reinvest' or 'fixed')")
            reinv_raw = sz_raw["reinvest"]
            fix_raw = sz_raw["fixed"]
            capital_cfg = CapitalConfig(
                start_balance_usd=float(cap_raw["start_balance_usd"]),
                leverage=int(sz_raw["leverage_display"]),
                max_open_positions=int(max_pos) if max_pos is not None else None,
                risk_per_trade_ratio=None,
                fixed_notional_usd=float(fix_raw["notional_usd"]),
                sizing_mode=sz_mode,
                reinvest_notional_equity_ratio=float(reinv_raw["notional_equity_ratio"]),
                reinvest_apply_signal_multipliers=bool(reinv_raw["apply_signal_multipliers"]),
                fixed_apply_signal_multipliers=bool(fix_raw["apply_signal_multipliers"]),
            )
        else:
            risk_ratio = cap_raw["risk_per_trade_ratio"]
            fixed_notional = cap_raw["fixed_notional_usd"]
            if (risk_ratio is None) == (fixed_notional is None):
                raise ValueError(
                    "Exactly one of capital.risk_per_trade_ratio or capital.fixed_notional_usd must be non-null"
                )
            capital_cfg = CapitalConfig(
                start_balance_usd=float(cap_raw["start_balance_usd"]),
                leverage=int(cap_raw["leverage"]),
                max_open_positions=int(max_pos) if max_pos is not None else None,
                risk_per_trade_ratio=float(risk_ratio) if risk_ratio is not None else None,
                fixed_notional_usd=float(fixed_notional) if fixed_notional is not None else None,
                sizing_mode="legacy_risk",
            )

        sl_buf = rm_raw["sl_buffer_ratio"]
        tp_rr = rm_raw["tp_rr_ratio"]
        mk_win = mk_raw["transition_window_days"]
        mk_min_long = mk_raw["min_net_score_for_long_ratio"]
        mk_min_short = mk_raw["min_net_score_for_short_ratio"]

        passport = StrategyPassport(
            strategy_id=str(raw["strategy_id"]),
            enabled=bool(raw["enabled"]),
            symbols=[str(s).upper().strip() for s in raw["symbols"]],
            timeframe=str(raw["timeframe"]).lower().strip(),
            capital=capital_cfg,
            signal=SignalPluginConfig(
                plugin=str(sig_raw["plugin"]).strip(),
                exit_on_opposite_signal=bool(sig_raw["exit_on_opposite_signal"]),
                params=dict(sig_raw["params"]),
            ),
            session_window=SessionWindowConfig(
                enabled=bool(ses_raw["enabled"]),
                timezone=str(ses_raw["timezone"]),
                start_time=str(ses_raw["start_time"]),
                end_time=str(ses_raw["end_time"]),
                weekdays_only=bool(ses_raw["weekdays_only"]),
            ),
            trend_fatigue_filter=TrendFatigueConfig(
                enabled=bool(fat_raw["enabled"]),
                lookback_days=int(fat_raw["lookback_days"]),
                min_net_efficiency_ratio=float(fat_raw["min_net_efficiency_ratio"]),
                max_counter_pullback_ratio=float(fat_raw["max_counter_pullback_ratio"]),
                block_mode=str(fat_raw["block_mode"]),
            ),
            ema_trend_filter=EmaTrendFilterConfig(
                enabled=bool(et_raw["enabled"]),
                period=int(et_raw["period"]),
            ),
            risk_management=RiskManagementConfig(
                sl_anchor=str(rm_raw["sl_anchor"]),
                sl_buffer_ratio=float(sl_buf) if sl_buf is not None else None,
                tp_rr_ratio=float(tp_rr) if tp_rr is not None else None,
                soft_stop_enabled=bool(rm_raw["soft_stop_enabled"]),
            ),
            markov_regime=MarkovRegimeConfig(
                enabled=bool(mk_raw["enabled"]),
                return_lookback_days=int(mk_raw["return_lookback_days"]),
                bull_return_threshold_ratio=float(mk_raw["bull_return_threshold_ratio"]),
                bear_return_threshold_ratio=float(mk_raw["bear_return_threshold_ratio"]),
                transition_window_days=int(mk_win) if mk_win is not None else None,
                min_history_states=int(mk_raw["min_history_states"]),
                forecast_horizon_days=int(mk_raw["forecast_horizon_days"]),
                laplace_alpha=float(mk_raw["laplace_alpha"]),
                block_long_in_bear=bool(mk_raw["block_long_in_bear"]),
                block_short_in_bull=bool(mk_raw["block_short_in_bull"]),
                min_net_score_for_long_ratio=float(mk_min_long) if mk_min_long is not None else None,
                min_net_score_for_short_ratio=float(mk_min_short) if mk_min_short is not None else None,
                dynamic_sizing_enabled=bool(mk_raw["dynamic_sizing_enabled"]),
                high_persistence_threshold_ratio=float(mk_raw["high_persistence_threshold_ratio"]),
                aligned_high_persistence_multiplier=float(mk_raw["aligned_high_persistence_multiplier"]),
                sideways_size_multiplier=float(mk_raw["sideways_size_multiplier"]),
                counter_regime_size_multiplier=float(mk_raw["counter_regime_size_multiplier"]),
            ),
            market_sentinel=MarketSentinelConfig(
                enabled=bool(ms_raw["enabled"]),
                benchmark_symbol=(
                    str(ms_raw["benchmark_symbol"]).upper().strip()
                    if ms_raw["benchmark_symbol"] is not None
                    else None
                ),
                btc_sentiment_enabled=bool(ms_raw["btc_sentiment_enabled"]),
                btc_ema_period=int(ms_raw["btc_ema_period"]),
                btc_adx_period=int(ms_raw["btc_adx_period"]),
                btc_chop_adx_threshold=float(ms_raw["btc_chop_adx_threshold"]),
                btc_ret_lookback_days=int(ms_raw["btc_ret_lookback_days"]),
                btc_chop_size_multiplier=float(ms_raw["btc_chop_size_multiplier"]),
                btc_counter_long_multiplier=float(ms_raw["btc_counter_long_multiplier"]),
                btc_counter_short_multiplier=float(ms_raw["btc_counter_short_multiplier"]),
                block_long_in_btc_bear=bool(ms_raw["block_long_in_btc_bear"]),
                breadth_enabled=bool(ms_raw["breadth_enabled"]),
                breadth_adx_period=int(ms_raw["breadth_adx_period"]),
                breadth_min_adx=float(ms_raw["breadth_min_adx"]),
                breadth_min_trending_coins=int(ms_raw["breadth_min_trending_coins"]),
                breadth_defensive_multiplier=float(ms_raw["breadth_defensive_multiplier"]),
            ),
            remora_gate=RemoraGateConfig(
                enabled=bool(rg_raw["enabled"]),
                mode=str(rg_raw["mode"]).strip(),
                min_grid_level=int(rg_raw["min_grid_level"]),
                min_adv_pct=float(rg_raw["min_adv_pct"]) if rg_raw["min_adv_pct"] is not None else None,
                allow_reversal_outside_pool=bool(rg_raw["allow_reversal_outside_pool"]),
                snapshot_path=(
                    str(rg_raw["snapshot_path"]).strip()
                    if rg_raw["snapshot_path"] is not None
                    else None
                ),
                max_staleness_ms=int(rg_raw["max_staleness_ms"]),
                dynamic_sizing=RemoraDynamicSizingConfig(
                    enabled=bool(rg_ds_raw["enabled"]),
                    min_slots=int(rg_ds_raw["min_slots"]),
                    max_slots=int(rg_ds_raw["max_slots"]),
                    min_slot_equity_pct=float(rg_ds_raw["min_slot_equity_pct"]),
                    max_slot_equity_pct=float(rg_ds_raw["max_slot_equity_pct"]),
                    equity_utilization=float(rg_ds_raw["equity_utilization"]),
                    margin_safety_ratio=float(rg_ds_raw["margin_safety_ratio"]),
                ),
            ),
            portfolio_risk=PortfolioRiskConfig(
                enabled=bool(pr_raw["enabled"]),
                max_same_side_positions=(
                    int(pr_raw["max_same_side_positions"])
                    if pr_raw["max_same_side_positions"] is not None
                    else None
                ),
                max_portfolio_risk_ratio=(
                    float(pr_raw["max_portfolio_risk_ratio"])
                    if pr_raw["max_portfolio_risk_ratio"] is not None
                    else None
                ),
                dd_throttle_enabled=bool(pr_raw["dd_throttle_enabled"]),
                dd_tier1_ratio=float(pr_raw["dd_tier1_ratio"]),
                dd_tier1_multiplier=float(pr_raw["dd_tier1_multiplier"]),
                dd_tier2_ratio=float(pr_raw["dd_tier2_ratio"]),
                dd_tier2_multiplier=float(pr_raw["dd_tier2_multiplier"]),
                funding_guard_enabled=bool(pr_raw["funding_guard_enabled"]),
                max_adverse_funding_rate=float(pr_raw["max_adverse_funding_rate"]),
                adverse_funding_multiplier=float(pr_raw["adverse_funding_multiplier"]),
            ),
        )
        StrictConfigLoader._validate_passport(passport)
        return passport

    @staticmethod
    def _validate_passport(p: StrategyPassport) -> None:
        if not p.symbols:
            raise ValueError("Strategy symbols list must not be empty")
        if p.capital.start_balance_usd <= 0.0:
            raise ValueError("start_balance_usd must be positive")
        if p.capital.leverage < 1:
            raise ValueError("leverage must be >= 1")
        if p.capital.sizing_mode not in ("reinvest", "fixed", "legacy_risk"):
            raise ValueError(f"Unsupported sizing_mode: {p.capital.sizing_mode}")
        if p.capital.sizing_mode == "reinvest":
            if p.capital.reinvest_notional_equity_ratio is None or p.capital.reinvest_notional_equity_ratio <= 0.0:
                raise ValueError("reinvest.notional_equity_ratio must be positive")
        elif p.capital.sizing_mode == "fixed":
            if p.capital.fixed_notional_usd is None or p.capital.fixed_notional_usd <= 0.0:
                raise ValueError("fixed.notional_usd must be positive")
        if p.risk_management.sl_anchor not in ("origin_swing_extreme", "none"):
            raise ValueError(f"Unsupported sl_anchor: {p.risk_management.sl_anchor}")
        if p.risk_management.tp_rr_ratio is not None and p.risk_management.tp_rr_ratio <= 0.0:
            raise ValueError("tp_rr_ratio must be positive or null")
        if p.risk_management.sl_buffer_ratio is not None and p.risk_management.sl_buffer_ratio <= 0.0:
            raise ValueError("sl_buffer_ratio must be positive or null")
        if p.risk_management.soft_stop_enabled:
            raise ValueError("soft_stop_enabled must be false per strategy specification")
        rg = p.remora_gate
        if rg.mode not in ("counter_hedge", "any_side"):
            raise ValueError(f"Unsupported remora_gate.mode: {rg.mode}")
        if rg.min_grid_level < 0:
            raise ValueError("remora_gate.min_grid_level must be >= 0")
        if rg.max_staleness_ms < 0:
            raise ValueError("remora_gate.max_staleness_ms must be >= 0")
        ds = rg.dynamic_sizing
        if ds.min_slots < 1 or ds.max_slots < ds.min_slots:
            raise ValueError("remora_gate.dynamic_sizing slot bounds must satisfy 1 <= min_slots <= max_slots")
        if not (0.0 < ds.min_slot_equity_pct <= ds.max_slot_equity_pct):
            raise ValueError("remora_gate.dynamic_sizing slot equity pct bounds invalid")
        if ds.equity_utilization <= 0.0:
            raise ValueError("remora_gate.dynamic_sizing.equity_utilization must be positive")
        if not (0.0 < ds.margin_safety_ratio <= 1.0):
            raise ValueError("remora_gate.dynamic_sizing.margin_safety_ratio must be in (0, 1]")
        pr = p.portfolio_risk
        if pr.max_same_side_positions is not None and pr.max_same_side_positions < 1:
            raise ValueError("portfolio_risk.max_same_side_positions must be >= 1 or null")
        if pr.max_portfolio_risk_ratio is not None and pr.max_portfolio_risk_ratio <= 0.0:
            raise ValueError("portfolio_risk.max_portfolio_risk_ratio must be > 0 or null")
        if not (0.0 < pr.dd_tier1_ratio <= pr.dd_tier2_ratio < 1.0):
            raise ValueError("portfolio_risk dd_tier ratios must satisfy 0 < tier1 <= tier2 < 1")
        if not (0.0 < pr.dd_tier2_multiplier <= pr.dd_tier1_multiplier <= 1.0):
            raise ValueError("portfolio_risk dd_tier multipliers must satisfy 0 < tier2 <= tier1 <= 1")
        if pr.max_adverse_funding_rate <= 0.0:
            raise ValueError("portfolio_risk.max_adverse_funding_rate must be positive")
        if not (0.0 < pr.adverse_funding_multiplier <= 1.0):
            raise ValueError("portfolio_risk.adverse_funding_multiplier must be in (0, 1]")
