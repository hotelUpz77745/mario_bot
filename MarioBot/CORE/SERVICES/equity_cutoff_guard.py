# Path: MarioBot/CORE/SERVICES/equity_cutoff_guard.py
# Role: Emergency Equity Cutoff — портфельный стоп-кран по просадке от High-Water Mark (закрытие + пауза + алерт TG)

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Set

from MarioBot.c_log import UnifiedLogger


@dataclass(frozen=True, slots=True)
class EquityCutoffConfig:
    """Строгая конфигурация портфельного стоп-крана из app.json['equity_cutoff'] (доли от 1: _ratio)."""
    enabled: bool
    max_drawdown_ratio: float
    close_open_positions_on_trip: bool
    pause_runtime_on_trip: bool
    notify_telegram: bool

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "EquityCutoffConfig":
        return cls(
            enabled=bool(raw["enabled"]),
            max_drawdown_ratio=float(raw["max_drawdown_ratio"]),
            close_open_positions_on_trip=bool(raw["close_open_positions_on_trip"]),
            pause_runtime_on_trip=bool(raw["pause_runtime_on_trip"]),
            notify_telegram=bool(raw["notify_telegram"]),
        )


class EmergencyEquityCutoffGuard:
    """
    Аварийный предохранитель капитала (Emergency Equity Cutoff):
    - Отслеживает High-Water Mark (пиковое equity) каждого активного слота;
    - Если просадка (HWM - equity) / HWM достигает max_drawdown_ratio (например, 0.25 = 25%):
      1. Закрывает все открытые позиции слота по рынку (reason='EQUITY_CUTOFF', если включено);
      2. Переводит рантайм в паузу (auto_start=false, если включено);
      3. Отправляет критический алерт в Telegram;
    - Снимается только осознанным ручным перезапуском через ▶️ Start (перекалибрует HWM на текущее equity).
    """

    def __init__(self, cfg: EquityCutoffConfig, runtime: Any):
        self.logger = UnifiedLogger("EquityCutoffGuard")
        self.cfg = cfg
        self.runtime = runtime
        self._hwm_by_slot: Dict[str, float] = {}
        self._tripped_slots: Set[str] = set()

    @property
    def is_tripped(self) -> bool:
        return bool(self._tripped_slots)

    def get_slot_drawdown_ratio(self, slot: Any) -> float:
        eq = max(0.0, float(slot.broker.get_equity_usd()))
        start_b = max(1e-9, float(slot.passport.capital.start_balance_usd))
        hwm = self._hwm_by_slot.get(slot.slot_id, max(start_b, eq))
        if eq > hwm:
            hwm = eq
            self._hwm_by_slot[slot.slot_id] = hwm
        else:
            self._hwm_by_slot.setdefault(slot.slot_id, hwm)
        return max(0.0, (hwm - eq) / hwm) if hwm > 0.0 else 0.0

    def reset_on_manual_resume(self, slots: List[Any]) -> None:
        """Снимает сработавший стоп-кран и перекалибровывает HWM на текущее equity при ручном ▶️ Start."""
        if not self._tripped_slots:
            return
        for slot in slots:
            if slot.slot_id in self._tripped_slots:
                cur_eq = max(1e-6, float(slot.broker.get_equity_usd()))
                self._hwm_by_slot[slot.slot_id] = cur_eq
                self.logger.warning(
                    f"[EQUITY_CUTOFF][{slot.slot_id}] Снят вручную; новый HWM откалиброван на ${cur_eq:,.2f}"
                )
        self._tripped_slots.clear()

    def evaluate_slots(self, slots: List[Any]) -> List[Dict[str, Any]]:
        """Проверяет просадку всех слотов (вызывается в 0.1s лупе). Возвращает список событий срабатывания."""
        if not self.cfg.enabled or self.cfg.max_drawdown_ratio <= 0.0:
            return []

        trips: List[Dict[str, Any]] = []
        for slot in slots:
            if slot.slot_id in self._tripped_slots:
                continue
            eq = max(0.0, float(slot.broker.get_equity_usd()))
            dd_ratio = self.get_slot_drawdown_ratio(slot)
            hwm = self._hwm_by_slot[slot.slot_id]
            if dd_ratio < self.cfg.max_drawdown_ratio:
                continue

            self._tripped_slots.add(slot.slot_id)
            closed_cnt = 0
            if self.cfg.close_open_positions_on_trip:
                for sym in list(slot.broker.get_all_open_positions().keys()):
                    if slot.broker.submit_close_market(sym, reason="EQUITY_CUTOFF"):
                        closed_cnt += 1

            if self.cfg.pause_runtime_on_trip and hasattr(self.runtime, "stop_trading"):
                self.runtime.stop_trading(persist_app_cfg=True)

            ev = {
                "slot_id": slot.slot_id,
                "hwm_usd": round(hwm, 2),
                "equity_usd": round(eq, 2),
                "dd_pct": round(dd_ratio * 100.0, 2),
                "limit_pct": round(self.cfg.max_drawdown_ratio * 100.0, 2),
                "closed_positions": closed_cnt,
            }
            trips.append(ev)
            self.logger.critical(
                f"[EQUITY_CUTOFF][{slot.slot_id}] СРАБОТАЛ СТОП-КРАН! "
                f"DD={ev['dd_pct']:.2f}% >= {ev['limit_pct']:.2f}% "
                f"(HWM=${hwm:,.2f} -> Eq=${eq:,.2f}), закрыто позиций={closed_cnt}"
            )
            if self.cfg.notify_telegram and hasattr(self.runtime, "_dispatch_tg_alert"):
                self.runtime._dispatch_tg_alert(
                    f"🛑 <b>[EMERGENCY EQUITY CUTOFF] Стоп-кран по слоту <code>{slot.slot_id}</code>!</b>\n\n"
                    f"• Пик (HWM): <code>${hwm:,.2f}</code> → Текущее Equity: <b>${eq:,.2f}</b>\n"
                    f"• Просадка: <b>-{ev['dd_pct']:.2f}%</b> (порог <code>-{ev['limit_pct']:.2f}%</code>)\n"
                    f"• Закрыто позиций MARKET: <code>{closed_cnt}</code>\n"
                    f"• Бот поставлен на <b>⏸️ PAUSED</b> (<code>auto_start=false</code>). Для возобновления нажмите ▶️ Start."
                )
        return trips
