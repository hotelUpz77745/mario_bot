# Path: MarioBacktest/ENGINE/vision_parser.py
# Role: Загрузчик и кэш-менеджер 1m свечей Binance Vision (data.binance.vision) в .npz

from __future__ import annotations

import io
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np


VISION_BASE_URL = "https://data.binance.vision"


@dataclass
class Aligned1mDataset:
    """Выровненная по 1m сетке матрица OHLCV (+ опционально прекомпилированный Funding Rate) для всех символов."""
    symbols: List[str]
    warmup_ts_ms: np.ndarray        # shape (W,) int64
    warmup_ohlcv: np.ndarray        # shape (W, S, 5) float64
    warmup_valid: np.ndarray        # shape (W, S) bool
    sim_ts_ms: np.ndarray           # shape (T,) int64
    sim_ohlcv: np.ndarray           # shape (T, S, 5) float64
    sim_valid: np.ndarray           # shape (T, S) bool
    sim_funding_rates: Optional[np.ndarray] = None  # shape (T, S) float64
    sim_funding_mask: Optional[np.ndarray] = None   # shape (T,) bool


def build_monthly_kline_url(symbol: str, year_month: str, interval: str = "1m") -> str:
    sym = symbol.upper().strip()
    return f"{VISION_BASE_URL}/data/futures/um/monthly/klines/{sym}/{interval}/{sym}-{interval}-{year_month}.zip"


def build_daily_kline_url(symbol: str, day_iso: str, interval: str = "1m") -> str:
    sym = symbol.upper().strip()
    return f"{VISION_BASE_URL}/data/futures/um/daily/klines/{sym}/{interval}/{sym}-{interval}-{day_iso}.zip"


def parse_kline_zip_bytes(zip_bytes: bytes) -> Tuple[np.ndarray, np.ndarray]:
    ts_list: List[int] = []
    ohlcv_list: List[Tuple[float, float, float, float, float]] = []

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            csv_names = zf.namelist()
        for name in sorted(csv_names):
            raw_text = zf.read(name).decode("utf-8", errors="replace")
            for line in raw_text.splitlines():
                s = line.strip()
                if not s or not s[0].isdigit():
                    continue
                parts = s.split(",")
                if len(parts) < 6:
                    continue
                raw_ts = int(parts[0])
                if raw_ts >= 100_000_000_000_000:
                    raw_ts //= 1000
                ts_list.append(raw_ts)
                ohlcv_list.append((
                    float(parts[1]),
                    float(parts[2]),
                    float(parts[3]),
                    float(parts[4]),
                    float(parts[5]),
                ))

    if not ts_list:
        return np.empty(0, dtype=np.int64), np.empty((0, 5), dtype=np.float64)

    ts_arr = np.asarray(ts_list, dtype=np.int64)
    ohlcv_arr = np.asarray(ohlcv_list, dtype=np.float64)
    order = np.argsort(ts_arr, kind="mergesort")
    ts_arr = ts_arr[order]
    ohlcv_arr = ohlcv_arr[order]

    if ts_arr.shape[0] > 1:
        uniq = np.empty(ts_arr.shape[0], dtype=bool)
        uniq[0] = True
        uniq[1:] = ts_arr[1:] != ts_arr[:-1]
        if not np.all(uniq):
            ts_arr = ts_arr[uniq]
            ohlcv_arr = ohlcv_arr[uniq]

    return ts_arr, ohlcv_arr


def iter_months_and_days(start_ms: int, end_exclusive_ms: int) -> Tuple[List[str], Dict[str, List[str]]]:
    start_dt = datetime.fromtimestamp(start_ms / 1000.0, tz=timezone.utc).date()
    end_dt = datetime.fromtimestamp((end_exclusive_ms - 1) / 1000.0, tz=timezone.utc).date()

    months: List[str] = []
    days_by_month: Dict[str, List[str]] = {}
    cur = start_dt
    one_day = timedelta(days=1)
    while cur <= end_dt:
        ym = f"{cur.year:04d}-{cur.month:02d}"
        if ym not in days_by_month:
            months.append(ym)
            days_by_month[ym] = []
        days_by_month[ym].append(cur.isoformat())
        cur += one_day
    return months, days_by_month


def _is_full_past_month(ym: str, today_utc: Optional[date] = None) -> bool:
    ref = today_utc or datetime.now(timezone.utc).date()
    y, m = (int(x) for x in ym.split("-"))
    return (y, m) < (ref.year, ref.month)


class BinanceVision1mParser:
    """
    Скачивает и кэширует 1m OHLCV свечи Binance Futures (USD-M) в сжатых .npz файлах.
    Поддерживает чтение уже скачанных файлов из extra_cache_dirs без повторной загрузки.
    """

    def __init__(
        self,
        cache_dir: Path,
        extra_cache_dirs: Optional[List[Path]] = None,
        max_workers: int = 16,
        http_timeout_sec: float = 30.0,
        http_retries: int = 3,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.extra_cache_dirs: List[Path] = [Path(p) for p in (extra_cache_dirs or [])]
        self.max_workers = max(1, int(max_workers))
        self.http_timeout_sec = float(http_timeout_sec)
        self.http_retries = max(1, int(http_retries))

    def _find_existing_file(self, filename: str) -> Optional[Path]:
        primary = self.cache_dir / filename
        if primary.exists():
            return primary
        for ext_dir in self.extra_cache_dirs:
            cand = ext_dir / filename
            if cand.exists():
                return cand
        return None

    def _download_bytes(self, url: str) -> Optional[bytes]:
        last_err: Optional[Exception] = None
        for _ in range(self.http_retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "MarioBacktest/1.0"})
                with urllib.request.urlopen(req, timeout=self.http_timeout_sec) as resp:
                    if resp.status == 200:
                        return resp.read()
            except urllib.error.HTTPError as he:
                if he.code == 404:
                    return None
                last_err = he
            except Exception as exc:
                last_err = exc
        if last_err is not None:
            raise RuntimeError(f"Failed downloading {url}: {last_err}")
        return None

    @staticmethod
    def _save_npz(path: Path, ts_ms: np.ndarray, ohlcv: np.ndarray) -> None:
        tmp = path.with_name(f"{path.name}.tmp.npz")
        np.savez_compressed(tmp, ts_ms=ts_ms, ohlcv=ohlcv)
        tmp.replace(path)

    @staticmethod
    def _load_npz(path: Path) -> Tuple[np.ndarray, np.ndarray]:
        with np.load(path, allow_pickle=False) as data:
            return data["ts_ms"].astype(np.int64), data["ohlcv"].astype(np.float64)

    def _fetch_unit(self, kind: str, symbol: str, key: str) -> Tuple[str, str, str, bool]:
        fname = f"{symbol.upper()}_1m_{kind}_{key}.npz"
        if self._find_existing_file(fname) is not None:
            return kind, symbol, key, True

        url = (
            build_monthly_kline_url(symbol, key, "1m")
            if kind == "M"
            else build_daily_kline_url(symbol, key, "1m")
        )
        raw = self._download_bytes(url)
        if raw is None:
            return kind, symbol, key, False
        ts_ms, ohlcv = parse_kline_zip_bytes(raw)
        self._save_npz(self.cache_dir / fname, ts_ms, ohlcv)
        return kind, symbol, key, True

    def ensure_cached_range(
        self,
        symbols: List[str],
        start_ms: int,
        end_exclusive_ms: int,
        skip_download: bool = False,
    ) -> None:
        if skip_download:
            return

        months, days_by_month = iter_months_and_days(start_ms, end_exclusive_ms)
        today_utc = datetime.now(timezone.utc).date()

        monthly_jobs: List[Tuple[str, str, str]] = []
        daily_jobs: List[Tuple[str, str, str]] = []

        for sym in symbols:
            sym_u = sym.upper().strip()
            for ym in months:
                if self._find_existing_file(f"{sym_u}_1m_M_{ym}.npz") is not None:
                    continue
                if _is_full_past_month(ym, today_utc):
                    monthly_jobs.append(("M", sym_u, ym))
                else:
                    for d_iso in days_by_month[ym]:
                        if self._find_existing_file(f"{sym_u}_1m_D_{d_iso}.npz") is None:
                            daily_jobs.append(("D", sym_u, d_iso))

        fallback_months: List[Tuple[str, str]] = []
        if monthly_jobs:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futs = [pool.submit(self._fetch_unit, k, s, ym) for (k, s, ym) in monthly_jobs]
                for fut in as_completed(futs):
                    _k, s, ym, ok = fut.result()
                    if not ok:
                        fallback_months.append((s, ym))

        for s, ym in fallback_months:
            for d_iso in days_by_month[ym]:
                if self._find_existing_file(f"{s}_1m_D_{d_iso}.npz") is None:
                    daily_jobs.append(("D", s, d_iso))

        if daily_jobs:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futs = [pool.submit(self._fetch_unit, k, s, d_iso) for (k, s, d_iso) in daily_jobs]
                for fut in as_completed(futs):
                    fut.result()

    def load_symbol_series(
        self,
        symbol: str,
        start_ms: int,
        end_exclusive_ms: int,
    ) -> Tuple[np.ndarray, np.ndarray]:
        sym_u = symbol.upper().strip()
        months, days_by_month = iter_months_and_days(start_ms, end_exclusive_ms)
        ts_chunks: List[np.ndarray] = []
        ohlcv_chunks: List[np.ndarray] = []

        for ym in months:
            m_path = self._find_existing_file(f"{sym_u}_1m_M_{ym}.npz")
            if m_path is not None:
                t_arr, o_arr = self._load_npz(m_path)
                if t_arr.shape[0] > 0:
                    ts_chunks.append(t_arr)
                    ohlcv_chunks.append(o_arr)
            else:
                for d_iso in days_by_month[ym]:
                    d_path = self._find_existing_file(f"{sym_u}_1m_D_{d_iso}.npz")
                    if d_path is not None:
                        t_arr, o_arr = self._load_npz(d_path)
                        if t_arr.shape[0] > 0:
                            ts_chunks.append(t_arr)
                            ohlcv_chunks.append(o_arr)

        if not ts_chunks:
            return np.empty(0, dtype=np.int64), np.empty((0, 5), dtype=np.float64)

        ts_all = np.concatenate(ts_chunks)
        ohlcv_all = np.concatenate(ohlcv_chunks, axis=0)
        mask = (ts_all >= start_ms) & (ts_all < end_exclusive_ms)
        ts_all = ts_all[mask]
        ohlcv_all = ohlcv_all[mask]

        if ts_all.shape[0] > 1:
            order = np.argsort(ts_all, kind="mergesort")
            ts_all = ts_all[order]
            ohlcv_all = ohlcv_all[order]
            uniq = np.empty(ts_all.shape[0], dtype=bool)
            uniq[0] = True
            uniq[1:] = ts_all[1:] != ts_all[:-1]
            if not np.all(uniq):
                ts_all = ts_all[uniq]
                ohlcv_all = ohlcv_all[uniq]

        return ts_all, ohlcv_all

    def build_aligned_dataset(
        self,
        symbols: List[str],
        warmup_start_ms: int,
        sim_start_ms: int,
        sim_end_exclusive_ms: int,
        skip_download: bool = False,
    ) -> Aligned1mDataset:
        clean_syms = [s.upper().strip() for s in symbols]
        self.ensure_cached_range(
            symbols=clean_syms,
            start_ms=warmup_start_ms,
            end_exclusive_ms=sim_end_exclusive_ms,
            skip_download=skip_download,
        )

        n_syms = len(clean_syms)
        warmup_ts = np.arange(warmup_start_ms, sim_start_ms, 60_000, dtype=np.int64)
        sim_ts = np.arange(sim_start_ms, sim_end_exclusive_ms, 60_000, dtype=np.int64)

        w_len = int(warmup_ts.shape[0])
        t_len = int(sim_ts.shape[0])

        warmup_ohlcv = np.zeros((w_len, n_syms, 5), dtype=np.float64)
        warmup_valid = np.zeros((w_len, n_syms), dtype=bool)

        sim_ohlcv = np.zeros((t_len, n_syms, 5), dtype=np.float64)
        sim_valid = np.zeros((t_len, n_syms), dtype=bool)

        for idx, sym in enumerate(clean_syms):
            ts_arr, ohlcv_arr = self.load_symbol_series(sym, warmup_start_ms, sim_end_exclusive_ms)
            if ts_arr.shape[0] == 0:
                continue

            if w_len > 0:
                w_mask = (ts_arr >= warmup_start_ms) & (ts_arr < sim_start_ms)
                w_ts = ts_arr[w_mask]
                w_vals = ohlcv_arr[w_mask]
                w_pos = ((w_ts - warmup_start_ms) // 60_000).astype(np.int64)
                ok_w = (w_pos >= 0) & (w_pos < w_len) & ((w_ts - warmup_start_ms) % 60_000 == 0)
                w_pos = w_pos[ok_w]
                warmup_ohlcv[w_pos, idx, :] = w_vals[ok_w]
                warmup_valid[w_pos, idx] = True

            s_mask = (ts_arr >= sim_start_ms) & (ts_arr < sim_end_exclusive_ms)
            s_ts = ts_arr[s_mask]
            s_vals = ohlcv_arr[s_mask]
            s_pos = ((s_ts - sim_start_ms) // 60_000).astype(np.int64)
            ok_s = (s_pos >= 0) & (s_pos < t_len) & ((s_ts - sim_start_ms) % 60_000 == 0)
            s_pos = s_pos[ok_s]
            sim_ohlcv[s_pos, idx, :] = s_vals[ok_s]
            sim_valid[s_pos, idx] = True

        return Aligned1mDataset(
            symbols=clean_syms,
            warmup_ts_ms=warmup_ts,
            warmup_ohlcv=warmup_ohlcv,
            warmup_valid=warmup_valid,
            sim_ts_ms=sim_ts,
            sim_ohlcv=sim_ohlcv,
            sim_valid=sim_valid,
        )
