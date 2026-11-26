# Path: MarioBacktest/ENGINE/funding_parser.py
# Role: Загрузчик и кэш-менеджер исторических 8h Funding Rates Binance Futures в .npz

from __future__ import annotations

import io
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Sequence, Tuple
import numpy as np
import requests


VISION_FUNDING_BASE = "https://data.binance.vision/data/futures/um/monthly/fundingRate"
FAPI_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"


def _iter_year_months(start_ms: int, end_ms: int) -> List[str]:
    s_dt = datetime.fromtimestamp(start_ms / 1000.0, tz=timezone.utc)
    e_dt = datetime.fromtimestamp(end_ms / 1000.0, tz=timezone.utc)
    y, m = s_dt.year, s_dt.month
    out: List[str] = []
    while (y, m) <= (e_dt.year, e_dt.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def _parse_funding_zip(zip_bytes: bytes) -> Tuple[List[int], List[float]]:
    ts_list: List[int] = []
    rate_list: List[float] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in sorted(zf.namelist()):
            text = zf.read(name).decode("utf-8", errors="replace")
            for line in text.splitlines():
                s = line.strip()
                if not s or not s[0].isdigit():
                    continue
                parts = s.split(",")
                if len(parts) < 3:
                    continue
                # Binance Vision fundingRate CSV: calc_time, funding_interval_hours, last_funding_rate
                ts_ms = int(parts[0])
                rate = float(parts[2])
                ts_list.append(ts_ms)
                rate_list.append(rate)
    return ts_list, rate_list


class BinanceFundingRateParser:
    """
    Скачивает и кэширует исторические 8-часовые funding rates (USD-M Futures)
    из архивов data.binance.vision (с догрузкой последнего месяца через fapi.binance.com).
    """

    def __init__(self, cache_dir: Path, max_workers: int = 8):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_workers = max(1, int(max_workers))

    def _cache_path(self, symbol: str) -> Path:
        return self.cache_dir / f"{symbol.upper().strip()}_funding.npz"

    def _download_month(self, symbol: str, ym: str) -> Tuple[List[int], List[float]]:
        sym = symbol.upper().strip()
        url = f"{VISION_FUNDING_BASE}/{sym}/{sym}-fundingRate-{ym}.zip"
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200 and resp.content:
                return _parse_funding_zip(resp.content)
        except Exception:
            pass
        return [], []

    def _download_rest_tail(self, symbol: str, start_ms: int, end_ms: int) -> Tuple[List[int], List[float]]:
        sym = symbol.upper().strip()
        ts_list: List[int] = []
        rate_list: List[float] = []
        cursor = int(start_ms)
        while cursor < end_ms:
            try:
                resp = requests.get(
                    FAPI_FUNDING_URL,
                    params={"symbol": sym, "startTime": cursor, "endTime": int(end_ms), "limit": 1000},
                    timeout=15,
                )
                if resp.status_code != 200:
                    break
                rows = resp.json()
                if not rows:
                    break
                last_t = cursor
                for r in rows:
                    t = int(r["fundingTime"])
                    ts_list.append(t)
                    rate_list.append(float(r["fundingRate"]))
                    last_t = t
                if len(rows) < 1000 or last_t <= cursor:
                    break
                cursor = last_t + 1
            except Exception:
                break
        return ts_list, rate_list

    def load_symbol_funding(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
        force_refresh: bool = False,
    ) -> Tuple[np.ndarray, np.ndarray]:
        sym = symbol.upper().strip()
        cpath = self._cache_path(sym)
        if cpath.exists() and not force_refresh:
            npz = np.load(cpath)
            ts_arr = npz["ts_ms"].astype(np.int64)
            rt_arr = npz["rate"].astype(np.float64)
            mask = (ts_arr >= start_ms) & (ts_arr <= end_ms)
            return ts_arr[mask], rt_arr[mask]

        months = _iter_year_months(start_ms, end_ms)
        all_ts: List[int] = []
        all_rt: List[float] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futs = [pool.submit(self._download_month, sym, ym) for ym in months]
            for fut in as_completed(futs):
                t_m, r_m = fut.result()
                all_ts.extend(t_m)
                all_rt.extend(r_m)

        # Догружаем хвост (текущий незакрытый месяц или пропуски) через REST API
        tail_start = (max(all_ts) + 1) if all_ts else start_ms
        if tail_start < end_ms:
            t_r, r_r = self._download_rest_tail(sym, tail_start, end_ms)
            all_ts.extend(t_r)
            all_rt.extend(r_r)

        if not all_ts:
            return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64)

        ts_arr = np.asarray(all_ts, dtype=np.int64)
        rt_arr = np.asarray(all_rt, dtype=np.float64)
        order = np.argsort(ts_arr, kind="mergesort")
        ts_arr = ts_arr[order]
        rt_arr = rt_arr[order]
        uniq = np.empty(ts_arr.shape[0], dtype=bool)
        uniq[0] = True
        uniq[1:] = ts_arr[1:] != ts_arr[:-1]
        ts_arr = ts_arr[uniq]
        rt_arr = rt_arr[uniq]

        np.savez_compressed(cpath, ts_ms=ts_arr, rate=rt_arr)
        mask = (ts_arr >= start_ms) & (ts_arr <= end_ms)
        return ts_arr[mask], rt_arr[mask]

    def load_universe_funding(
        self,
        symbols: Sequence[str],
        start_ms: int,
        end_ms: int,
    ) -> Dict[str, Tuple[np.ndarray, np.ndarray]]:
        out: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
        for sym in symbols:
            out[sym.upper().strip()] = self.load_symbol_funding(sym, start_ms, end_ms)
        return out
