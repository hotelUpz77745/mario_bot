# Path: ClavaBot/c_log.py
# Role: Потокобезопасный логгер с поддержкой тихого режима бэктеста (CLAVA_BACKTEST=1)

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional


class UnifiedLogger:
    """
    Унифицированный логгер для ClavaBot и ClavaBacktest.
    При переменной окружения CLAVA_BACKTEST=1 подавляет консольный и файловый вывод ниже ERROR.
    """

    def __init__(self, name: str, log_file: Optional[Path] = None, level: int = logging.INFO):
        self._name = name
        self._silent_backtest = os.environ.get("CLAVA_BACKTEST", "0") == "1"
        self._logger = logging.getLogger(f"clava.{name}")
        self._logger.propagate = False

        if self._silent_backtest:
            self._logger.setLevel(logging.ERROR)
        else:
            self._logger.setLevel(level)

        if not self._logger.handlers:
            fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
            stream_handler = logging.StreamHandler(sys.stdout)
            stream_handler.setFormatter(fmt)
            self._logger.addHandler(stream_handler)

            if log_file is not None and not self._silent_backtest:
                log_file.parent.mkdir(parents=True, exist_ok=True)
                file_handler = logging.FileHandler(log_file, encoding="utf-8")
                file_handler.setFormatter(fmt)
                self._logger.addHandler(file_handler)

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
