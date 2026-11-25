# Path: ClavaBot/tests/test_remora_hybrid.py
# Role: Юнит-тесты гибридной прилипалы: GridRemoraGate (вход только по пулу+уровню, выход по паспорту)
#       и RemoraDynamicSizer (концентрированный динамический сайз на стороне Маркова).

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ClavaBot.CORE.config_loader import (
    RemoraDynamicSizingConfig,
    RemoraGateConfig,
    StrictConfigLoader,
)
from ClavaBot.CORE.models import Candle, SignalEvent
from ClavaBot.CORE.orchestrator import TradeEngineOrchestrator
from ClavaBot.CORE.spec_manager import SpecManager
from ClavaBot.CORE.STRATEGY.base_signal_plugin import BaseSignalPlugin
from ClavaBot.CORE.STRATEGY.grid_remora_gate import (
    CoinGridState,
    GridRemoraGate,
    InMemoryGridStateProvider,
    JsonSnapshotGridStateProvider,
)
from ClavaBot.CORE.STRATEGY.remora_dynamic_sizer import RemoraDynamicSizer
from ClavaBacktest.ENGINE.sim_broker import SimExecutionBroker


class _ScriptedPlugin(BaseSignalPlugin):
    """Тестовый плагин, выдающий заданные сигналы/выходы по close_ts_ms."""

    def __init__(self) -> None:
        self.signals: dict[tuple[str, int], str] = {}
        self.exits: dict[tuple[str, int], str] = {}

    def update_and_evaluate(self, symbol: str, candle: Candle):
        side = self.signals.get((symbol, candle.close_ts_ms))
        if side is None:
            return None
        sl = candle.close * (0.9 if side == "LONG" else 1.1)
        return SignalEvent(
            symbol=symbol,
            side=side,
            signal_ts_ms=candle.close_ts_ms,
            entry_ref_price=candle.close,
            origin_extreme_price=sl,
            size_multiplier=1.0,
        )

    def check_exit(self, symbol: str, candle: Candle, open_position):
        return self.exits.get((symbol, candle.close_ts_ms))


def _remora_passport(raw_base: dict, min_level: int = 3, leverage: int = 4, util: float = 2.0):
    raw = copy.deepcopy(raw_base)
    raw["symbols"] = ["ADAUSDT", "SOLUSDT", "ETHUSDT"]
    raw["capital"]["start_balance_usd"] = 1435.0
    raw["capital"]["leverage"] = leverage
    raw["capital"]["fixed_notional_usd"] = 143.5
    raw["trend_fatigue_filter"]["enabled"] = False
    raw["markov_regime"]["enabled"] = False
    raw["market_sentinel"]["enabled"] = False
    raw["remora_gate"] = {
        "enabled": True,
        "mode": "counter_hedge",
        "min_grid_level": min_level,
        "min_adv_pct": None,
        "allow_reversal_outside_pool": False,
        "snapshot_path": None,
        "max_staleness_ms": 60_000,
        "dynamic_sizing": {
            "enabled": True,
            "min_slots": 3,
            "max_slots": 6,
            "min_slot_equity_pct": 0.25,
            "max_slot_equity_pct": 0.70,
            "equity_utilization": util,
            "margin_safety_ratio": 0.90,
        },
    }
    return StrictConfigLoader.parse_strategy_passport(raw)


class TestRemoraHybrid(unittest.TestCase):
    def setUp(self) -> None:
        app_cfg = StrictConfigLoader.read_json(ROOT_DIR / "ClavaBot" / "CFG" / "app.json")
        self.spec_manager = SpecManager(app_cfg["symbol_specs"])
        self.raw_base = StrictConfigLoader.read_json(
            ROOT_DIR / "ClavaBot" / "CFG" / "strategies" / "markov_pure_4h_gmm_conf_antichop_exit_sw_sl5.json"
        )

    def test_gate_counter_hedge_and_staleness_fail_closed(self) -> None:
        provider = InMemoryGridStateProvider()
        cfg = RemoraGateConfig(
            enabled=True,
            mode="counter_hedge",
            min_grid_level=3,
            min_adv_pct=9.0,
            allow_reversal_outside_pool=False,
            snapshot_path=None,
            max_staleness_ms=10_000,
            dynamic_sizing=RemoraDynamicSizingConfig(True, 3, 6, 0.25, 0.70, 2.0, 0.9),
        )
        gate = GridRemoraGate(cfg, provider)
        now_ms = 1_700_000_000_000
        provider.set_snapshot({
            "ADAUSDT": CoinGridState("ADAUSDT", True, long_level=0, short_level=3, long_adv_pct=0.0, short_adv_pct=11.0, updated_ts_ms=now_ms),
            "SOLUSDT": CoinGridState("SOLUSDT", False, long_level=5, short_level=5, long_adv_pct=25.0, short_adv_pct=25.0, updated_ts_ms=now_ms),
        })
        self.assertTrue(gate.can_open_entry("ADAUSDT", "LONG", now_ms + 5_000))
        self.assertFalse(gate.can_open_entry("ADAUSDT", "SHORT", now_ms + 5_000))
        self.assertFalse(gate.can_open_entry("SOLUSDT", "LONG", now_ms + 5_000))
        self.assertFalse(gate.can_open_entry("ADAUSDT", "LONG", now_ms + 15_000))

    def test_dynamic_sizer_scales_concentration_and_respects_margin(self) -> None:
        cfg = RemoraDynamicSizingConfig(
            enabled=True,
            min_slots=3,
            max_slots=6,
            min_slot_equity_pct=0.25,
            max_slot_equity_pct=0.70,
            equity_utilization=2.0,
            margin_safety_ratio=0.90,
        )
        sizer = RemoraDynamicSizer(leverage=4, cfg=cfg, spec_manager=self.spec_manager)
        # При equity=1435, util=2.0 и 1 триггере (min_slots=3): 1435*2/3 = 956.67, capped at 0.70*1435 = 1004.5 -> 956.67
        q1 = sizer.calculate_qty("ETHUSDT", equity_usd=1435.0, free_margin_usd=1435.0, entry_price=2000.0, open_positions_count=0, triggered_pool_count=1, size_multiplier=1.0)
        # Когда открыто уже 3 позы и триггернулась 4-я (effective=4): 1435*2/4 = 717.5 -> меньше, чем q1
        q4 = sizer.calculate_qty("ETHUSDT", equity_usd=1435.0, free_margin_usd=1000.0, entry_price=2000.0, open_positions_count=3, triggered_pool_count=1, size_multiplier=1.0)
        self.assertGreater(q1, q4)
        self.assertAlmostEqual(q1 * 2000.0, 956.0, delta=10.0)
        self.assertAlmostEqual(q4 * 2000.0, 717.5, delta=10.0)

    def test_open_position_survives_pool_exit_and_closes_on_opposite_without_reopen(self) -> None:
        p = _remora_passport(self.raw_base, min_level=3, leverage=4, util=2.0)
        broker = SimExecutionBroker(1435.0, leverage=4, taker_fee_rate=0.0005, maker_fee_rate=0.0002, slippage_rate=0.0, tp_slippage_rate=0.0)
        provider = InMemoryGridStateProvider()
        plugin = _ScriptedPlugin()
        orch = TradeEngineOrchestrator(p, self.spec_manager, broker, signal_plugin=plugin, grid_state_provider=provider)

        t1, t2, t3 = 1_700_000_000_000, 1_700_014_400_000, 1_700_028_800_000
        # t1: ADAUSDT в пуле HRON_3, SHORT в просадке L3 -> разрешён LONG в Clava
        provider.set_snapshot({"ADAUSDT": CoinGridState("ADAUSDT", True, long_level=0, short_level=3, updated_ts_ms=t1)})
        plugin.signals[("ADAUSDT", t1)] = "LONG"
        orch.on_candle_close("ADAUSDT", Candle("ADAUSDT", t1 - 14_400_000, t1, 1.0, 1.0, 1.0, 1.0, 100.0))
        broker.fill_pending_at_minute_open("ADAUSDT", t1, 1.0)
        self.assertEqual(broker.get_open_position_side("ADAUSDT"), "LONG")
        pos = broker.get_open_position("ADAUSDT")
        self.assertGreater(pos.qty * pos.entry_price, 700.0)

        # t2: ротация HRON_3 выбросила ADAUSDT из пула, но Clava держит позицию в трекере и ведёт её дальше!
        provider.set_snapshot({"SOLUSDT": CoinGridState("SOLUSDT", True, long_level=0, short_level=0, updated_ts_ms=t2)})
        self.assertIn("ADAUSDT", orch.get_tracking_symbols(t2, ["ADAUSDT", "SOLUSDT"]))
        orch.on_candle_close("ADAUSDT", Candle("ADAUSDT", t1, t2, 1.0, 1.2, 1.0, 1.2, 100.0))
        self.assertEqual(broker.get_open_position_side("ADAUSDT"), "LONG")

        # t3: приходит OPPOSITE_SIGNAL (SHORT) -> Clava закрывает LONG, но новый SHORT не открывает (ADA вне пула!)
        plugin.signals[("ADAUSDT", t3)] = "SHORT"
        orch.on_candle_close("ADAUSDT", Candle("ADAUSDT", t2, t3, 1.2, 1.2, 1.1, 1.15, 100.0))
        broker.fill_pending_at_minute_open("ADAUSDT", t3, 1.15)
        self.assertIsNone(broker.get_open_position("ADAUSDT"))
        self.assertEqual(len(broker.closed_trades), 1)
        self.assertEqual(broker.closed_trades[0].exit_reason, "OPPOSITE_SIGNAL")

    def test_json_snapshot_provider(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "hron3_grid_state.json"
            fp.write_text(json.dumps({
                "updated_ts_ms": 1_700_000_000_000,
                "symbols": {"MYXUSDT": {"in_active_pool": True, "long_level": 0, "short_level": 4, "short_adv_pct": 16.5}},
            }), encoding="utf-8")
            prov = JsonSnapshotGridStateProvider(fp)
            snap = prov.get_snapshot(1_700_000_010_000)
            self.assertIsNotNone(snap)
            self.assertEqual(snap["MYXUSDT"].short_level, 4)


if __name__ == "__main__":
    unittest.main()
