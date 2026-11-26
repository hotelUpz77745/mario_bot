# Path: MarioBot/TG/keyboards.py
# Role: Reply и Inline клавиатуры панели управления MarioBot / MarioPaper (по образцу HRON_3)

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Главная Reply-клавиатура: Start, Status, Stop, Analytics, Logs."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="▶️ Start"),
                KeyboardButton(text="ℹ️ Status"),
                KeyboardButton(text="⏸️ Stop"),
            ],
            [
                KeyboardButton(text="📊 Analytics"),
                KeyboardButton(text="📜 Logs"),
            ],
        ],
        resize_keyboard=True,
    )


def get_analytics_inline_keyboard() -> InlineKeyboardMarkup:
    """Inline-кнопки карточки аналитики 1-й стратегии."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📝 Trades Ledger (.csv)", callback_data="mario_an_csv"),
                InlineKeyboardButton(text="📄 Summary (.txt)", callback_data="mario_an_txt"),
            ],
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="mario_an_refresh"),
            ],
        ]
    )


def get_logs_inline_keyboard() -> InlineKeyboardMarkup:
    """Inline-меню выгрузки логов и рантайм-бэкапа (по образцу HRON_3)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📄 1. Срез лога (хвост 1000 строк)", callback_data="mario_logs_tail")],
            [InlineKeyboardButton(text="📦 2. Текущий лог в ZIP (all.log)", callback_data="mario_logs_zip")],
            [InlineKeyboardButton(text="📚 3. Полный архив всех ротаций (.zip)", callback_data="mario_logs_all")],
            [InlineKeyboardButton(text="💾 4. Runtime Backup (.json)", callback_data="mario_logs_backup")],
        ]
    )
