# Path: MarioBot/API/__init__.py
# Role: Домен внешних API-адаптеров MarioBot (Binance Futures REST/WS, локальный IPC-хаб, загрузчик .env)

from MarioBot.API.env_loader import load_dotenv_file

__all__ = ["load_dotenv_file"]
