# Path: MarioBot/API/BINANCE/cms_client.py
# Role: Публичный REST-адаптер новостного CMS Binance (Контур 2 детектора делистинга)

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests

from MarioBot.API.BINANCE.rest_client import HttpTransportFn


class BinanceCmsNewsClient:
    """
    Изолированный клиент новостного CMS Binance (домен www.binance.com):
    - читает каталог объявлений (catalogId=161 — делистинги/фьючерсы) и детальные статьи;
    - в режиме бэктеста (MARIO_BACKTEST=1) — жёсткий no-op (возвращает None без сетевых вызовов);
    - поддерживает инъекцию transport для детерминированных юнит-тестов.
    """

    CMS_BASE_URL = "https://www.binance.com"
    CMS_CATALOG_PATH = "/bapi/composite/v1/public/cms/article/catalog/list/query"
    CMS_DETAIL_PATH = "/bapi/composite/v1/public/cms/article/detail/query"

    def __init__(self, transport: Optional[HttpTransportFn] = None):
        self._session = requests.Session()
        self._transport = transport

    @staticmethod
    def _is_backtest() -> bool:
        return os.environ.get("MARIO_BACKTEST") == "1"

    def _get_json(self, path: str, params: Dict[str, Any], timeout_sec: float) -> Optional[Dict[str, Any]]:
        if self._is_backtest():
            return None
        url = f"{self.CMS_BASE_URL}{path}"
        headers = {"User-Agent": "MarioBot/1.0", "Accept": "application/json"}
        if self._transport is not None:
            data = self._transport("GET", url, headers, params, float(timeout_sec))
            return data if isinstance(data, dict) else None
        resp = self._session.get(url, headers=headers, params=params, timeout=float(timeout_sec))
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None

    def get_cms_articles(
        self,
        catalog_id: int,
        page_no: int,
        page_size: int,
        timeout_sec: float,
    ) -> Optional[List[Dict[str, Any]]]:
        """Возвращает сырой список статей каталога Binance CMS или None."""
        data = self._get_json(
            self.CMS_CATALOG_PATH,
            params={"catalogId": int(catalog_id), "pageNo": int(page_no), "pageSize": int(page_size)},
            timeout_sec=timeout_sec,
        )
        if not isinstance(data, dict):
            return None
        inner = data.get("data")
        if isinstance(inner, dict):
            articles = inner.get("articles")
            if isinstance(articles, list):
                return articles
            catalogs = inner.get("catalogs")
            if isinstance(catalogs, list) and catalogs and isinstance(catalogs[0], dict):
                cat_articles = catalogs[0].get("articles")
                if isinstance(cat_articles, list):
                    return cat_articles
        return None

    def get_cms_article_detail(self, article_code: str, timeout_sec: float) -> Optional[Dict[str, Any]]:
        """Возвращает payload детальной статьи анонса по её articleCode или None."""
        data = self._get_json(
            self.CMS_DETAIL_PATH,
            params={"articleCode": str(article_code)},
            timeout_sec=timeout_sec,
        )
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            return data["data"]
        return None
