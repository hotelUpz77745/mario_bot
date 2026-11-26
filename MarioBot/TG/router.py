# Path: MarioBot/TG/router.py
# Role: Роутер Telegram-команд MarioBot / MarioPaper (идемпотентные Start/Stop/Status, Analytics 1-й стратегии, Logs)

from __future__ import annotations

from typing import Any, Optional

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, FSInputFile, Message

from MarioBot.c_log import UnifiedLogger
from MarioBot.TG.keyboards import (
    get_analytics_inline_keyboard,
    get_logs_inline_keyboard,
    get_main_keyboard,
)
from MarioBot.TG.log_export_service import LogExportService


class MarioTelegramRouter:
    """
    Роутер панели управления Mario (Dumb Executor поверх MarioClusterRuntime):
    - Start / Stop / Status полностью идемпотентны;
    - Analytics строго по первой активной стратегии из active_strategies (runtime.slots[0]);
    - Logs — хвост 1000 строк, ZIP, ротации и Runtime Backup (.json).
    """

    def __init__(self, runtime: Any, log_service: LogExportService):
        self.runtime = runtime
        self.log_service = log_service
        self.logger = UnifiedLogger("TGMarioRouter")
        self.router = Router(name="mario_router")
        self._register_routes()

    def _register_routes(self) -> None:
        r = self.router
        r.message(Command("start"))(self.on_status)
        r.message(Command("status"))(self.on_status)
        r.message(Command("analytics"))(self.on_analytics)
        r.message(Command("logs"))(self.on_logs)
        r.message(F.text == "▶️ Start")(self.on_start_trading)
        r.message(F.text == "⏸️ Stop")(self.on_stop_trading)
        r.message(F.text == "ℹ️ Status")(self.on_status)
        r.message(F.text == "📊 Analytics")(self.on_analytics)
        r.message(F.text == "📜 Logs")(self.on_logs)
        r.callback_query(F.data == "mario_an_refresh")(self.cb_analytics_refresh)
        r.callback_query(F.data == "mario_an_csv")(self.cb_analytics_csv)
        r.callback_query(F.data == "mario_an_txt")(self.cb_analytics_txt)
        r.callback_query(F.data == "mario_logs_tail")(self.cb_logs_tail)
        r.callback_query(F.data == "mario_logs_zip")(self.cb_logs_zip)
        r.callback_query(F.data == "mario_logs_all")(self.cb_logs_all)
        r.callback_query(F.data == "mario_logs_backup")(self.cb_logs_backup)

    def _primary_slot(self) -> Optional[Any]:
        return self.runtime.slots[0] if getattr(self.runtime, "slots", None) else None

    def format_status_text(self) -> str:
        rt = self.runtime
        mode = getattr(rt, "mode", "PROD")
        dry_run = bool(rt.app_cfg["binance_futures"]["dry_run"])
        mode_badge = f"PAPER" if mode == "PAPER" else ("PROD [DRY_RUN]" if dry_run else "PROD [LIVE HEDGE]")
        run_badge = "⏸️ Paused" if getattr(rt, "is_paused", False) else "▶️ Running"
        ipc_role = getattr(getattr(rt, "price_hub", None), "role", "UNKNOWN")
        ipc_syms = len(getattr(getattr(rt, "price_hub", None), "cluster_symbols", []))

        lines = [
            f"🍄 <b>Mario Control Panel</b> (<code>{mode_badge}</code>)",
            f"Статус торговли: <b>{run_badge}</b>",
            f"IPC Price Stream: <b>{ipc_role}</b> (<code>{ipc_syms}</code> символов)",
            f"Игровая лупа SL/TP: <code>{getattr(rt, 'game_loop_interval_sec', 0.1):.2f}s</code>",
        ]

        slot = self._primary_slot()
        if slot is not None:
            cap = slot.passport.capital
            if cap.sizing_mode == "reinvest":
                sz_desc = f"REINVEST ({cap.reinvest_notional_equity_ratio * 100.0:.1f}% eq)"
            elif cap.sizing_mode == "fixed":
                sz_desc = f"FIXED (${cap.fixed_notional_usd:.2f})"
            else:
                sz_desc = cap.sizing_mode.upper()

            eq = slot.broker.get_equity_usd()
            open_pos = slot.broker.get_all_open_positions()
            last_px = getattr(slot.broker, "last_prices", {})
            lines.append("")
            lines.append(f"🎯 <b>Стратегия #1:</b> <code>{slot.slot_id}</code>")
            lines.append(f"Сайзинг: <code>{sz_desc}</code> | Lev (display): <code>{cap.leverage}x</code>")
            lines.append(f"Equity: <b>${eq:,.2f}</b> | Открыто позиций: <b>{len(open_pos)}/{cap.max_open_positions}</b>")
            if open_pos:
                lines.append("")
                lines.append("📌 <b>Открытые позиции:</b>")
                for sym, pos in sorted(open_pos.items()):
                    px = float(last_px.get(sym, pos.entry_price))
                    upnl = ((px - pos.entry_price) * pos.qty) if pos.side == "LONG" else ((pos.entry_price - px) * pos.qty)
                    sl_s = f"{pos.stop_loss:.6g}" if pos.stop_loss is not None else "—"
                    lines.append(
                        f"• <b>{sym}</b> <code>{pos.side}</code> qty=<code>{pos.qty}</code> "
                        f"in=<code>{pos.entry_price:.6g}</code> now=<code>{px:.6g}</code> "
                        f"SL=<code>{sl_s}</code> uPnL=<b>${upnl:+.2f}</b>"
                    )
        return "\n".join(lines)

    def format_primary_analytics_text(self) -> str:
        slot = self._primary_slot()
        if slot is None:
            return "⚠️ Активные стратегии не найдены."
        slot.analytics.update_open_positions(
            slot.broker.get_all_open_positions(),
            getattr(slot.broker, "last_prices", {}),
            slot.broker.get_equity_usd(),
        )
        st = slot.analytics.state
        open_cnt = int(st["open_longs"]) + int(st["open_shorts"])
        hold_min = st["hold_min_h"] if st["hold_min_h"] is not None else 0.0
        return (
            f"📊 <b>Аналитика стратегии #1</b> (<code>{st['mode']}</code>)\n"
            f"ID: <code>{st['strategy_id']}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Старт: <code>${st['start_balance_usd']:,.2f}</code> → Equity: <b>${st['current_equity_usd']:,.2f}</b>\n"
            f"Net PnL: <b>${st['net_pnl_usd']:+,.2f}</b> (<b>{st['roi_pct']:+.2f}%</b>)\n"
            f"MaxDD: <code>{st['max_drawdown_pct']:.2f}%</code> (<code>${st['max_drawdown_usd']:,.2f}</code>) | "
            f"MinEq: <code>${st['min_equity_usd']:,.2f}</code>\n"
            f"Сделок: <b>{st['trades_total']}</b> | "
            f"WR: <b>{st['winrate_pct']:.1f}%</b> | PF: <b>{st['profit_factor']:.2f}</b>\n"
            f"Комиссии: <code>-${abs(st['total_fees_usd']):,.2f}</code> | Funding: <code>${st['total_funding_usd']:+,.2f}</code>\n"
            f"Удержание (ч): min <code>{hold_min}</code> / avg <code>{st['hold_avg_h']}</code> / max <code>{st['hold_max_h']}</code>\n"
            f"Ср. сделка: <code>${st['avg_trade_pnl_usd']:+.2f}</code> (<code>{st['avg_trade_roi_pct']:+.2f}%</code>)\n"
            f"Лучшая: <code>${st['best_trade_usd']:+.2f}</code> | Худшая: <code>${st['worst_trade_usd']:+.2f}</code>\n"
            f"Max TP%: <code>+{st['max_tp_move_pct']:.2f}%</code> | Max SL%: <code>-{st['max_sl_exec_pct']:.2f}%</code>\n"
            f"Открыто сейчас: <b>{open_cnt}</b> (<code>{st['open_longs']}L / {st['open_shorts']}S</code>)"
        )

    async def on_start_trading(self, message: Message) -> None:
        changed = self.runtime.start_trading(persist_app_cfg=True)
        if not changed:
            await message.answer(
                "ℹ️ Торговля уже запущена (<b>▶️ Running</b>).",
                reply_markup=get_main_keyboard(),
                parse_mode="HTML",
            )
            return
        await message.answer(
            "✅ <b>Торговля запущена!</b>\nСтатус: <b>▶️ Running</b> (<code>auto_start=true</code> сохранён).",
            reply_markup=get_main_keyboard(),
            parse_mode="HTML",
        )

    async def on_stop_trading(self, message: Message) -> None:
        changed = self.runtime.stop_trading(persist_app_cfg=True)
        if not changed:
            await message.answer(
                "ℹ️ Торговля уже на паузе (<b>⏸️ Paused</b>).",
                reply_markup=get_main_keyboard(),
                parse_mode="HTML",
            )
            return
        await message.answer(
            "⏸️ <b>Торговля поставлена на паузу</b> (<code>auto_start=false</code> сохранён).\n"
            "Новые входы заблокированы. Открытые позиции и системные триггеры SL/TP продолжают контролироваться.",
            reply_markup=get_main_keyboard(),
            parse_mode="HTML",
        )

    async def on_status(self, message: Message) -> None:
        await message.answer(
            self.format_status_text(),
            reply_markup=get_main_keyboard(),
            parse_mode="HTML",
        )

    async def on_analytics(self, message: Message) -> None:
        await message.answer(
            self.format_primary_analytics_text(),
            reply_markup=get_analytics_inline_keyboard(),
            parse_mode="HTML",
        )

    async def cb_analytics_refresh(self, callback: CallbackQuery) -> None:
        await callback.answer("Обновлено")
        try:
            await callback.message.edit_text(
                self.format_primary_analytics_text(),
                reply_markup=get_analytics_inline_keyboard(),
                parse_mode="HTML",
            )
        except Exception:
            pass

    async def cb_analytics_csv(self, callback: CallbackQuery) -> None:
        await callback.answer()
        slot = self._primary_slot()
        if slot is None:
            return
        slot.analytics.flush()
        csv_path = slot.analytics.ledger_path
        if not csv_path.exists():
            await callback.message.answer("⚠️ Файл сделок ещё пуст.")
            return
        await callback.message.answer_document(
            FSInputFile(str(csv_path), filename=csv_path.name),
            caption=f"📝 <b>Trades Ledger (.csv)</b>: <code>{slot.slot_id}</code>",
            parse_mode="HTML",
        )

    async def cb_analytics_txt(self, callback: CallbackQuery) -> None:
        await callback.answer()
        slot = self._primary_slot()
        if slot is None:
            return
        slot.analytics.flush()
        txt_path = slot.analytics.report_path
        if not txt_path.exists():
            await callback.message.answer("⚠️ Файл сводки ещё не создан.")
            return
        await callback.message.answer_document(
            FSInputFile(str(txt_path), filename=txt_path.name),
            caption=f"📄 <b>Summary Report (.txt)</b>: <code>{slot.slot_id}</code>",
            parse_mode="HTML",
        )

    async def on_logs(self, message: Message) -> None:
        await message.answer(
            "📜 <b>Выберите выгрузку логов или бэкап рантайма:</b>",
            reply_markup=get_logs_inline_keyboard(),
            parse_mode="HTML",
        )

    async def cb_logs_tail(self, callback: CallbackQuery) -> None:
        await callback.answer("Готовлю срез 1000 строк...")
        await self.log_service.send_part_tail(callback, max_lines=1000)

    async def cb_logs_zip(self, callback: CallbackQuery) -> None:
        await callback.answer("Упаковываю all.log в ZIP...")
        await self.log_service.send_current_zip(callback)

    async def cb_logs_all(self, callback: CallbackQuery) -> None:
        await callback.answer("Собираю все ротации логов...")
        await self.log_service.send_all_archives(callback)

    async def cb_logs_backup(self, callback: CallbackQuery) -> None:
        await callback.answer("Создаю Runtime Backup...")
        await self.log_service.send_runtime_backup(callback)
