# Path: MarioBot/c_log.py
# Role: Потокобезопасный логгер с ротацией по строкам (logs/all.log, как в HRON_3) и тихим режимом MARIO_BACKTEST=1

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional


class LineRotatingFileHandler(logging.Handler):
    """Ротирует logs/all.log -> all.log.1 .. all.log.N при достижении max_lines строк (как в HRON_3)."""

    def __init__(self, log_path: Path, max_lines: int = 5000, backup_count: int = 5):
        super().__init__()
        self.log_path = Path(log_path)
        self.max_lines = max(100, int(max_lines))
        self.backup_count = max(1, int(backup_count))
        self._lock = threading.Lock()
        self._line_count = 0
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.log_path.exists():
            try:
                with self.log_path.open("r", encoding="utf-8", errors="replace") as fh:
                    self._line_count = sum(1 for _ in fh)
            except Exception:
                self._line_count = 0

    def _rotate_locked(self) -> None:
        for idx in range(self.backup_count, 0, -1):
            src = self.log_path if idx == 1 else self.log_path.with_name(f"{self.log_path.name}.{idx - 1}")
            dst = self.log_path.with_name(f"{self.log_path.name}.{idx}")
            if src.exists():
                if dst.exists():
                    try:
                        dst.unlink()
                    except Exception:
                        pass
                try:
                    os.replace(src, dst)
                except Exception:
                    pass
        self._line_count = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            with self._lock:
                if self._line_count >= self.max_lines:
                    self._rotate_locked()
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(msg + "\n")
                self._line_count += 1
        except Exception:
            self.handleError(record)


_FILE_HANDLER: Optional[LineRotatingFileHandler] = None
_LOG_TO_CONSOLE: bool = True
_GLOBAL_LEVEL: int = logging.INFO
_CONFIG_LOCK = threading.Lock()


def configure_file_logging(
    logs_dir: Path,
    level: str = "INFO",
    max_log_lines: int = 5000,
    backup_count: int = 5,
    log_to_console: bool = True,
    log_to_file: bool = True,
) -> Optional[Path]:
    """Инициализирует единый ротируемый лог logs/all.log для всех UnifiedLogger процесса."""
    global _FILE_HANDLER, _LOG_TO_CONSOLE, _GLOBAL_LEVEL
    if os.environ.get("MARIO_BACKTEST", "0") == "1":
        return None
    lvl = getattr(logging, str(level).upper().strip(), logging.INFO)
    log_path = Path(logs_dir) / "all.log"
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")

    with _CONFIG_LOCK:
        _GLOBAL_LEVEL = lvl
        _LOG_TO_CONSOLE = bool(log_to_console)
        if log_to_file:
            handler = LineRotatingFileHandler(log_path, max_lines=max_log_lines, backup_count=backup_count)
            handler.setFormatter(fmt)
            _FILE_HANDLER = handler
        else:
            _FILE_HANDLER = None

        # Подключаем файловый хэндлер ко всем уже созданным логгерам mario.*
        for lname, lg in list(logging.root.manager.loggerDict.items()):
            if isinstance(lg, logging.Logger) and lname.startswith("mario."):
                lg.setLevel(lvl)
                lg.handlers = [h for h in lg.handlers if not isinstance(h, LineRotatingFileHandler)]
                if _FILE_HANDLER is not None:
                    lg.addHandler(_FILE_HANDLER)
    return log_path if log_to_file else None


class UnifiedLogger:
    """
    Унифицированный логгер для MarioBot, MarioPaper и MarioBacktest.
    При переменной окружения MARIO_BACKTEST=1 подавляет консольный и файловый вывод ниже ERROR.
    """

    def __init__(self, name: str, log_file: Optional[Path] = None, level: Optional[int] = None):
        self._name = name
        self._silent_backtest = os.environ.get("MARIO_BACKTEST", "0") == "1"
        self._logger = logging.getLogger(f"mario.{name}")
        self._logger.propagate = False

        eff_level = _GLOBAL_LEVEL if level is None else level
        if self._silent_backtest:
            self._logger.setLevel(logging.ERROR)
        else:
            self._logger.setLevel(eff_level)

        if not self._logger.handlers:
            fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
            if _LOG_TO_CONSOLE:
                stream_handler = logging.StreamHandler(sys.stdout)
                stream_handler.setFormatter(fmt)
                self._logger.addHandler(stream_handler)

            if log_file is not None and not self._silent_backtest:
                log_file.parent.mkdir(parents=True, exist_ok=True)
                file_handler = logging.FileHandler(log_file, encoding="utf-8")
                file_handler.setFormatter(fmt)
                self._logger.addHandler(file_handler)

        if _FILE_HANDLER is not None and not self._silent_backtest:
            if not any(isinstance(h, LineRotatingFileHandler) for h in self._logger.handlers):
                self._logger.addHandler(_FILE_HANDLER)

    def debug(self, msg: str) -> None:
        if not self._silent_backtest:
            self._logger.debug(msg)

    def info(self, msg: str) -> None:
        if not self._silent_backtest:
            self._logger.info(msg)

    def warning(self, msg: str) -> None:
        if not self._silent_backtest:
            self._logger.warning(msg)

    def error(self, msg: str) -> None:
        self._logger.error(msg)
