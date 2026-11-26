# Path: MarioBot/API/env_loader.py
# Role: Загрузчик секретов из корневого .env без внешних зависимостей

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv_file(env_path: str | os.PathLike[str]) -> int:
    """Лёгкий парсер .env: заполняет только отсутствующие переменные окружения (без перезаписи системных)."""
    p = Path(env_path)
    if not p.exists():
        return 0
    loaded = 0
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        val = v.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val
            loaded += 1
    return loaded
