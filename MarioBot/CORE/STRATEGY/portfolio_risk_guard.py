# Path: MarioBot/CORE/STRATEGY/portfolio_risk_guard.py
# Role: Портфельный риск-контроллер (лимит однонаправленных позиций, кап открытого риска, DD-throttle и funding-guard)

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional
from MarioBot.CORE.config_loader import PortfolioRiskConfig
from MarioBot.CORE.models import OpenPosition, SideType


@dataclass(frozen=True, slots=True)
class PortfolioRiskDecision:
    """Результат проверки портфельных ограничений перед открытием новой сделки."""
    allowed: bool
    size_multiplier: float
    drawdown_ratio: float
    open_risk_ratio: float
    same_side_count: int
    block_reason: Optional[str] = None


class PortfolioRiskGuard:
    """
    Слой портфельного управления риском поверх одиночных сигналов:
    1. Лимит позиций в одном направлении (max_same_side_positions) — защита от синхронного обвала альтов.
    2. Кап суммарного открытого стоп-риска (max_portfolio_risk_ratio) — ограничение хвоста потерь.
    3. Ступенчатый Drawdown Throttle от High-Water Mark (HWM) капитала.
    4. Фильтр перегретого фандинга (funding_guard_enabled) — сжатие объёма против экстремального funding rate.
    """

    def __init__(self, cfg: PortfolioRiskConfig, start_equity_usd: float):
        self.cfg = cfg
        self.hwm_equity_usd: float = max(0.0, float(start_equity_usd))
        self._latest_funding_rate: Dict[str, float] = {}

    def update_funding_rate(self, symbol: str, funding_rate: float) -> None:
        self._latest_funding_rate[symbol.upper().strip()] = float(funding_rate)

    def update_equity(self, equity_usd: float) -> float:
        eq = float(equity_usd)
        if eq > self.hwm_equity_usd:
            self.hwm_equity_usd = eq
        if self.hwm_equity_usd <= 0.0:
            return 0.0
        return max(0.0, (self.hwm_equity_usd - eq) / self.hwm_equity_usd)

    def evaluate(
        self,
        symbol: str,
        side: SideType,
        equity_usd: float,
        open_positions: Dict[str, OpenPosition],
        exclude_symbol: Optional[str] = None,
    ) -> PortfolioRiskDecision:
        dd_ratio = self.update_equity(equity_usd)
        if not self.cfg.enabled:
            return PortfolioRiskDecision(True, 1.0, dd_ratio, 0.0, 0, None)

        excl = exclude_symbol.upper().strip() if exclude_symbol is not None else None
        same_side = 0
        open_risk_usd = 0.0
        for sym, pos in open_positions.items():
            if excl is not None and sym.upper().strip() == excl:
                continue
            if pos.side == side:
                same_side += 1
            if pos.stop_loss is not None and pos.qty > 0.0:
                open_risk_usd += abs(pos.entry_price - pos.stop_loss) * pos.qty

        open_risk_ratio = (open_risk_usd / equity_usd) if equity_usd > 0.0 else 0.0

        if self.cfg.max_same_side_positions is not None and same_side >= self.cfg.max_same_side_positions:
            return PortfolioRiskDecision(
                False, 0.0, dd_ratio, open_risk_ratio, same_side, "MAX_SAME_SIDE_POSITIONS"
            )

        if self.cfg.max_portfolio_risk_ratio is not None and open_risk_ratio >= self.cfg.max_portfolio_risk_ratio:
            return PortfolioRiskDecision(
                False, 0.0, dd_ratio, open_risk_ratio, same_side, "MAX_PORTFOLIO_RISK"
            )

        mult = 1.0
        if self.cfg.dd_throttle_enabled:
            if dd_ratio >= self.cfg.dd_tier2_ratio:
                mult *= self.cfg.dd_tier2_multiplier
            elif dd_ratio >= self.cfg.dd_tier1_ratio:
                mult *= self.cfg.dd_tier1_multiplier

        if self.cfg.funding_guard_enabled:
            fr = self._latest_funding_rate.get(symbol.upper().strip())
            if fr is not None:
                adverse = (side == "LONG" and fr >= self.cfg.max_adverse_funding_rate) or (
                    side == "SHORT" and fr <= -self.cfg.max_adverse_funding_rate
                )
                if adverse:
                    mult *= self.cfg.adverse_funding_multiplier

        return PortfolioRiskDecision(True, mult, dd_ratio, open_risk_ratio, same_side, None)
