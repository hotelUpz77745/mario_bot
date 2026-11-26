# Path: MarioBot/TG/log_export_service.py
# Role: Сервис выгрузки хвоста логов, ZIP-архивов ротаций и Runtime Backup в Telegram (по образцу HRON_3)

from __future__ import annotations

import asyncio
import glob
import os
import zipfile
from collections import deque
from pathlib import Path
from typing import List, Optional

from aiogram.exceptions import TelegramNetworkError
from aiogram.types import CallbackQuery, FSInputFile

from MarioBot.c_log import UnifiedLogger
from MarioBot.CORE.EXECUTION.runtime_state_store import MarioRuntimeStateStore


class LogExportService:
    """Инкапсулирует чтение хвоста, ZIP-упаковку и отправку логов/бэкапов с сетевым повтором."""

    def __init__(self, logs_dir: Path, runtime_store: Optional[MarioRuntimeStateStore] = None):
        self.logs_dir = Path(logs_dir)
        self.runtime_store = runtime_store
        self.logger = UnifiedLogger("TGLogExportService")

    @staticmethod
    def create_zip(files: List[Path], target_zip_path: Path) -> bool:
        try:
            target_zip_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(target_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for fpath in files:
                    if fpath.exists():
                        zf.write(fpath, arcname=fpath.name)
            return True
        except Exception:
            return False

    @staticmethod
    def extract_log_tail(source_path: Path, target_path: Path, max_lines: int = 1000) -> bool:
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with source_path.open("r", encoding="utf-8", errors="replace") as fh:
                lines = deque(fh, maxlen=max_lines)
            with target_path.open("w", encoding="utf-8") as fh:
                fh.writelines(lines)
            return True
        except Exception:
            return False

    @staticmethod
    async def send_document_with_retry(
        callback: CallbackQuery,
        file_path: Path,
        filename: str,
        caption: str,
        retries: int = 2,
        request_timeout: int = 60,
    ) -> None:
        last_err: Optional[Exception] = None
        for attempt in range(retries):
            try:
                await callback.message.answer_document(
                    FSInputFile(str(file_path), filename=filename),
                    caption=caption,
                    parse_mode="HTML",
                    request_timeout=request_timeout,
                )
                return
            except Exception as exc:
                last_err = exc
                if isinstance(exc, (TelegramNetworkError, asyncio.TimeoutError)) and attempt + 1 < retries:
                    await asyncio.sleep(1.0)
                    continue
                raise
        if last_err is not None:
            raise last_err

    async def send_part_tail(self, callback: CallbackQuery, max_lines: int = 1000) -> None:
        log_path = self.logs_dir / "all.log"
        if not log_path.exists():
            await callback.message.answer("⚠️ Файл logs/all.log ещё не создан.")
            return
        tail_path = self.logs_dir / f"recent_tail_{max_lines}.log"
        if not self.extract_log_tail(log_path, tail_path, max_lines=max_lines):
            await callback.message.answer("❌ Не удалось извлечь срез лога.")
            return
        try:
            kb_sz = tail_path.stat().st_size / 1024.0
            await self.send_document_with_retry(
                callback,
                tail_path,
                filename=tail_path.name,
                caption=f"📄 <b>Срез лога (последние {max_lines} строк)</b>\nРазмер: <code>{kb_sz:.1f} KB</code>",
            )
        except Exception as exc:
            zip_tail = self.logs_dir / f"recent_tail_{max_lines}.zip"
            if self.create_zip([tail_path], zip_tail):
                await self.send_document_with_retry(
                    callback,
                    zip_tail,
                    filename=zip_tail.name,
                    caption=f"📦 <b>Срез лога в ZIP ({max_lines} строк)</b> (резервная отправка)",
                )
                return
            await callback.message.answer(f"❌ Ошибка отправки лога: {exc}")

    async def send_current_zip(self, callback: CallbackQuery) -> None:
        log_path = self.logs_dir / "all.log"
        if not log_path.exists():
            await callback.message.answer("⚠️ Файл logs/all.log не найден.")
            return
        zip_path = self.logs_dir / "current_all_log.zip"
        if self.create_zip([log_path], zip_path):
            orig_kb = log_path.stat().st_size / 1024.0
            zip_kb = zip_path.stat().st_size / 1024.0
            await self.send_document_with_retry(
                callback,
                zip_path,
                filename=zip_path.name,
                caption=f"📦 <b>Текущий лог в ZIP</b>\n<code>{orig_kb:.1f} KB</code> → <code>{zip_kb:.1f} KB</code>",
            )
        else:
            await callback.message.answer("❌ Не удалось создать ZIP-архив.")

    async def send_all_archives(self, callback: CallbackQuery) -> None:
        log_files = [Path(p) for p in sorted(glob.glob(str(self.logs_dir / "all.log*")))]
        if not log_files:
            await callback.message.answer("⚠️ Файлы логов logs/all.log* не найдены.")
            return
        zip_path = self.logs_dir / "all_rotations_history.zip"
        if self.create_zip(log_files, zip_path):
            zip_kb = zip_path.stat().st_size / 1024.0
            names = ", ".join(p.name for p in log_files)
            await self.send_document_with_retry(
                callback,
                zip_path,
                filename=zip_path.name,
                caption=f"📚 <b>Все ротации логов (.zip)</b>\nФайлы: <code>{names}</code>\nРазмер: <code>{zip_kb:.1f} KB</code>",
            )
        else:
            await callback.message.answer("❌ Не удалось собрать архив ротаций.")

    async def send_runtime_backup(self, callback: CallbackQuery) -> None:
        if self.runtime_store is None:
            await callback.message.answer("⚠️ RuntimeStore не подключён.")
            return
        backup_file = self.runtime_store.export_backup()
        kb_sz = backup_file.stat().st_size / 1024.0
        await self.send_document_with_retry(
            callback,
            backup_file,
            filename=backup_file.name,
            caption=f"💾 <b>Runtime Backup (.json)</b>\nФайл: <code>{backup_file.name}</code> ({kb_sz:.1f} KB)",
        )
