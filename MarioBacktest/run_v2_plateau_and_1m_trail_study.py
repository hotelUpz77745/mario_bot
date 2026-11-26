# Path: MarioBacktest/run_v2_plateau_and_1m_trail_study.py
# Role: Точечное R&D без переподгонки: 3x3 плато V2 (act 8/10/12 x dist 3.0/3.5/4.0) и сравнение 4H-close vs 1m-tick трейлинга

from __future__ import annotations

import dataclasses
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

os.environ["MARIO_BACKTEST"] = "1"
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from MarioBot.ANALYTICS.report_formatter import ReportFormatter
from MarioBot.CORE.config_loader import StrategyPassport, StrictConfigLoader
from MarioBacktest.ENGINE.funding_parser import BinanceFundingRateParser
from MarioBacktest.ENGINE.tournament_runner import TournamentRunner
from MarioBacktest.ENGINE.vision_parser import Aligned1mDataset


def _attach_funding(dataset: Aligned1mDataset, funding_dir: Path) -> Aligned1mDataset:
    f_parser = BinanceFundingRateParser(funding_dir)
    if dataset.sim_ts_ms.shape[0] == 0:
        return dataset
    start_ms = int(dataset.sim_ts_ms[0])
    end_ms = int(dataset.sim_ts_ms[-1]) + 60_000
    t_len = int(dataset.sim_ts_ms.shape[0])
    s_len = len(dataset.symbols)
    rates = np.zeros((t_len, s_len), dtype=np.float64)
    mask = np.zeros(t_len, dtype=bool)
    raw = f_parser.load_universe_funding(dataset.symbols, start_ms, end_ms)
    for col, sym in enumerate(dataset.symbols):
        ts_arr, rt_arr = raw.get(sym, (np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64)))
        if ts_arr.shape[0] == 0:
            continue
        idx = np.searchsorted(dataset.sim_ts_ms, ts_arr, side="left")
        valid_i = idx < t_len
        idx = idx[valid_i]
        rt_v = rt_arr[valid_i]
        rates[idx, col] = rt_v
        mask[idx] = True
    return dataclasses.replace(dataset, sim_funding_rates=rates, sim_funding_mask=mask)


def _clone_passport(
    base: StrategyPassport,
    label: str,
    act: float | None,
    dist: float | None,
    mode: str = "4h_close",
) -> StrategyPassport:
    new_params = dict(base.signal.params)
    new_params["profit_trail_activation_atr_mult"] = act
    new_params["profit_trail_distance_atr_mult"] = dist
    new_params["profit_trail_mode"] = mode
    new_sig = dataclasses.replace(base.signal, params=new_params)
    return dataclasses.replace(base, strategy_id=label, signal=new_sig)


def _slice_subperiod_dataset(
    full_ds: Aligned1mDataset,
    start_date: str,
    warmup_days: int = 220,
) -> Aligned1mDataset:
    """Мгновенно вырезает подпериод (например 6M + 220д прогрева) из уже загруженного 5Y датасета без повторного чтения диска."""
    sim_start_ms = int(datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    warm_start_ms = sim_start_ms - int(warmup_days) * 86_400_000
    ts = full_ds.sim_ts_ms
    w_i = int(np.searchsorted(ts, warm_start_ms, side="left"))
    s_i = int(np.searchsorted(ts, sim_start_ms, side="left"))
    return Aligned1mDataset(
        symbols=full_ds.symbols,
        warmup_ts_ms=ts[w_i:s_i],
        warmup_ohlcv=full_ds.sim_ohlcv[w_i:s_i],
        warmup_valid=full_ds.sim_valid[w_i:s_i],
        sim_ts_ms=ts[s_i:],
        sim_ohlcv=full_ds.sim_ohlcv[s_i:],
        sim_valid=full_ds.sim_valid[s_i:],
        sim_funding_rates=full_ds.sim_funding_rates[s_i:] if full_ds.sim_funding_rates is not None else None,
        sim_funding_mask=full_ds.sim_funding_mask[s_i:] if full_ds.sim_funding_mask is not None else None,
    )


def _extract_metrics(tr: TournamentRunner, passport: StrategyPassport, ds: Aligned1mDataset) -> Dict[str, Any]:
    summary = tr.runner.run_passport(passport, ds)
    start_b = float(summary["start_balance_usd"])
    end_eq = float(summary["end_equity_usd"])
    roi = ((end_eq - start_b) / start_b) * 100.0
    max_dd = float(summary["max_drawdown_pct"])
    days = max(1.0, (int(ds.sim_ts_ms[-1]) - int(ds.sim_ts_ms[0])) / 86_400_000.0)
    years = days / 365.0
    cagr = ((max(end_eq, 1.0) / start_b) ** (1.0 / years) - 1.0) * 100.0 if years >= 0.8 else roi
    geom_calmar = (cagr / max_dd) if max_dd > 0.0 else 0.0
    diag = summary["trade_diagnostics"]
    return {
        "label": passport.strategy_id,
        "end_equity_usd": round(end_eq, 2),
        "roi_pct": round(roi, 2),
        "cagr_pct": round(cagr, 1),
        "max_dd_pct": round(max_dd, 2),
        "min_equity_usd": round(float(summary["min_equity_usd"]), 2),
        "geom_calmar": round(geom_calmar, 2),
        "trades": int(summary["total_trades"]),
        "wr_pct": round(float(summary["winrate_pct"]), 1),
        "pf": round(float(summary["profit_factor"]), 2),
        "avg_hold_h": round(float(diag["avg_hold_hours"]), 1),
        "fees_usd": round(-abs(float(summary["total_fees_usd"])), 2),
        "funding_usd": round(float(summary["total_funding_usd"]), 2),
        "exit_reasons": summary["exit_reasons"],
    }


def main() -> None:
    bt_dir = Path(__file__).resolve().parent
    tr = TournamentRunner(bt_dir)
    bot_dir = tr.bot_dir
    app_cfg = tr.app_cfg
    primary_slot = app_cfg["active_strategies"]["markov_alpha_top14_main"]
    strat_dir = bot_dir / str(app_cfg["paths"]["strategies_dir"])
    v2_path = strat_dir / "markov_pure_4h_gmm_conf_antichop_exit_sw_sl5_v2_profit_trail10.json"
    base_passport = StrictConfigLoader.load_strategy_passport(v2_path, slot_cfg=primary_slot, slot_id="v2_study")

    all_syms = sorted(set(base_passport.symbols) | {"BTCUSDT"})
    funding_dir = bt_dir / "CACHE" / "funding"

    print("Loading 5Y (2021-11-01..2026-11-18) 1m + real funding dataset...", flush=True)
    ds_5y = _attach_funding(
        tr.load_dataset_for_dates(all_syms, "2021-11-01", "2026-11-18", warmup_days=220, skip_download=True),
        funding_dir,
    )
    ds_6m = _slice_subperiod_dataset(ds_5y, "2026-05-19", warmup_days=220)
    print(f"Loaded! 5Y bars={len(ds_5y.sim_ts_ms):,} | 6M bars={len(ds_6m.sim_ts_ms):,}", flush=True)

    # 1. BASE anchor
    base_p = _clone_passport(base_passport, "BASE_NO_TRAIL", None, None, "4h_close")
    base_res = {
        "5y": _extract_metrics(tr, base_p, ds_5y),
        "6m": _extract_metrics(tr, base_p, ds_6m),
    }
    print(
        f"[BASE] 5Y=${base_res['5y']['end_equity_usd']:,.2f} DD={base_res['5y']['max_dd_pct']}% "
        f"Calmar={base_res['5y']['geom_calmar']} | 6M=${base_res['6m']['end_equity_usd']:,.2f} DD={base_res['6m']['max_dd_pct']}%",
        flush=True,
    )

    # 2. 3x3 Plateau Grid (4h_close)
    acts = [8.0, 10.0, 12.0]
    dists = [3.0, 3.5, 4.0]
    plateau: List[Dict[str, Any]] = []
    print("\n=== PART 1: V2 3x3 PLATEAU GRID (4H-CLOSE) ===", flush=True)
    for a in acts:
        for d in dists:
            lbl = f"V2_4H_A{int(a)}_D{d:.1f}"
            p = _clone_passport(base_passport, lbl, a, d, "4h_close")
            r5 = _extract_metrics(tr, p, ds_5y)
            r6 = _extract_metrics(tr, p, ds_6m)
            plateau.append({"act": a, "dist": d, "mode": "4h_close", "5y": r5, "6m": r6})
            print(
                f"  {lbl:16s} | 5Y: ${r5['end_equity_usd']:9,.2f} CAGR={r5['cagr_pct']:5.1f}% "
                f"DD={r5['max_dd_pct']:5.2f}% Calmar={r5['geom_calmar']:4.2f} PF={r5['pf']:4.2f} "
                f"avgHold={r5['avg_hold_h']/24:4.1f}d | 6M: ${r6['end_equity_usd']:7,.2f} DD={r6['max_dd_pct']:5.2f}%",
                flush=True,
            )

    # 3. 1m Intrabar Tick Trail vs 4H-Close on key points
    tick_pairs: List[Tuple[float, float]] = [(8.0, 3.0), (10.0, 3.5), (10.0, 4.0), (12.0, 4.0)]
    tick_vs_4h: List[Dict[str, Any]] = []
    print("\n=== PART 2: 1M INTRABAR (TICK) vs 4H-CLOSE PROFIT TRAIL ===", flush=True)
    for a, d in tick_pairs:
        lbl = f"V2_1M_A{int(a)}_D{d:.1f}"
        p = _clone_passport(base_passport, lbl, a, d, "intrabar_tick")
        r5 = _extract_metrics(tr, p, ds_5y)
        r6 = _extract_metrics(tr, p, ds_6m)
        tick_vs_4h.append({"act": a, "dist": d, "mode": "intrabar_tick", "5y": r5, "6m": r6})
        print(
            f"  {lbl:16s} | 5Y: ${r5['end_equity_usd']:9,.2f} CAGR={r5['cagr_pct']:5.1f}% "
            f"DD={r5['max_dd_pct']:5.2f}% Calmar={r5['geom_calmar']:4.2f} PF={r5['pf']:4.2f} "
            f"avgHold={r5['avg_hold_h']/24:4.1f}d | 6M: ${r6['end_equity_usd']:7,.2f} DD={r6['max_dd_pct']:5.2f}% "
            f"| exits={r5['exit_reasons']}",
            flush=True,
        )

    out_path = tr.reports_dir / "v2_plateau_and_1m_trail_study.json"
    ReportFormatter.export_json(
        out_path,
        {
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "sizing": "reinvest_12pct_start_1000",
            "base": base_res,
            "plateau_4h_3x3": plateau,
            "intrabar_1m_comparison": tick_vs_4h,
        },
    )
    print(f"\nSaved report -> {out_path}")


if __name__ == "__main__":
    main()
