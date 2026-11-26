# Path: MarioBot/API/BINANCE/__init__.py
# Role: Публичный интерфейс домена Binance USD-M Futures (REST-клиент и WebSocket-стример цен)

from MarioBot.API.BINANCE.rest_client import (
    BinanceFuturesRestClient,
    BinanceRestConfig,
    HttpTransportFn,
    format_decimal_str,
)
from MarioBot.API.BINANCE.ws_price_stream import BinanceHotPriceStream, PriceTick

__all__ = [
    "BinanceRestConfig",
    "BinanceFuturesRestClient",
    "HttpTransportFn",
    "format_decimal_str",
    "BinanceHotPriceStream",
    "PriceTick",
]
