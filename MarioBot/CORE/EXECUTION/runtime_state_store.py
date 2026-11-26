# Path: MarioBot/CORE/EXECUTION/runtime_state_store.py
# Role: Пер-символьное атомарное хранение рантайма (CFG/runtime/<slot>/<sym>.json), рестарт-ресторация и бэкап

from __future__ import annotations

import datetime
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from MarioBot.c_log import UnifiedLogger
from MarioBot.CORE.models import OpenPosition


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Атомарная запись JSON на диск (temp -> os.replace) с защитой от битых файлов."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, path)


def default_symbol_runtime(symbol: str, slot_id: str) -> Dict[str, Any]:
    """Чистая дефолтная структура пер-символьного рантайма (при отсутствии позиции)."""
    return {
        "symbol": symbol.upper().strip(),
        "slot_id": str(slot_id),
        "in_position": False,
        "side": None,
        "qty": 0.0,
        "entry_price": 0.0,
        "entry_ts_ms": 0,
        "stop_loss": None,
        "take_profit": None,
        "entry_fee_usd": 0.0,
        "last_updated_ts_ms": 0,
    }


def position_to_runtime_dict(pos: OpenPosition, slot_id: str, now_ms: Optional[int] = None) -> Dict[str, Any]:
    ts = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    return {
        "symbol": pos.symbol.upper().strip(),
        "slot_id": str(slot_id),
        "in_position": True,
        "side": pos.side,
        "qty": float(pos.qty),
        "entry_price": float(pos.entry_price),
        "entry_ts_ms": int(pos.entry_ts_ms),
        "stop_loss": float(pos.stop_loss) if pos.stop_loss is not None else None,
        "take_profit": float(pos.take_profit) if pos.take_profit is not None else None,
        "entry_fee_usd": float(pos.entry_fee_usd),
        "last_updated_ts_ms": ts,
    }


class MarioRuntimeStateStore:
    """
    Менеджер рантайма по образцу HRON_3 (CFG/runtime/<slot_id>/<symbol>.json):
    - прозрачные пер-символьные JSON-файлы позиций + _slot_state.json;
    - атомарная запись при каждом изменении позиции / баланса;
    - сброс символа в дефолтную структуру при отсутствии позиции на бирже;
    - On-Demand экспорт единого JSON-бэкапа для Telegram.
    """

    def __init__(self, runtime_dir: Path):
        self.runtime_dir = Path(runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.logger = UnifiedLogger("MarioRuntimeStore")

    def slot_dir(self, slot_id: str) -> Path:
        return self.runtime_dir / str(slot_id)

    def symbol_path(self, slot_id: str, symbol: str) -> Path:
        return self.slot_dir(slot_id) / f"{symbol.lower().strip()}.json"

    def slot_meta_path(self, slot_id: str) -> Path:
        return self.slot_dir(slot_id) / "_slot_state.json"

    def load_slot_meta(self, slot_id: str, start_balance_usd: float) -> Dict[str, Any]:
        path = self.slot_meta_path(slot_id)
        default_meta = {
            "slot_id": str(slot_id),
            "start_balance_usd": float(start_balance_usd),
            "realized_balance_usd": float(start_balance_usd),
            "last_funding_bucket": -1,
            "last_updated_ts_ms": int(time.time() * 1000),
        }
        if not path.exists():
            atomic_write_json(path, default_meta)
            return default_meta
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {
                "slot_id": str(slot_id),
                "start_balance_usd": float(raw["start_balance_usd"]),
                "realized_balance_usd": float(raw["realized_balance_usd"]),
                "last_funding_bucket": int(raw["last_funding_bucket"]),
                "last_updated_ts_ms": int(raw["last_updated_ts_ms"]),
            }
        except Exception as exc:
            self.logger.warning(f"[{slot_id}] Corrupt _slot_state.json ({exc}); resetting to default")
            atomic_write_json(path, default_meta)
            return default_meta

    def save_slot_meta(
        self,
        slot_id: str,
        start_balance_usd: float,
        realized_balance_usd: float,
        last_funding_bucket: int = -1,
    ) -> None:
        payload = {
            "slot_id": str(slot_id),
            "start_balance_usd": round(float(start_balance_usd), 6),
            "realized_balance_usd": round(float(realized_balance_usd), 6),
            "last_funding_bucket": int(last_funding_bucket),
            "last_updated_ts_ms": int(time.time() * 1000),
        }
        atomic_write_json(self.slot_meta_path(slot_id), payload)

    def load_slot_positions(self, slot_id: str, symbols: Iterable[str]) -> Dict[str, OpenPosition]:
        """Загружает активные позиции из CFG/runtime/<slot_id>/<sym>.json (недостающие создаёт в дефолте)."""
        out: Dict[str, OpenPosition] = {}
        for raw_sym in symbols:
            sym = raw_sym.upper().strip()
            path = self.symbol_path(slot_id, sym)
            if not path.exists():
                atomic_write_json(path, default_symbol_runtime(sym, slot_id))
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if bool(data.get("in_position")) and float(data.get("qty", 0.0)) > 0.0:
                    side = str(data["side"]).upper()
                    if side not in ("LONG", "SHORT"):
                        raise ValueError(f"Invalid side: {side}")
                    out[sym] = OpenPosition(
                        symbol=sym,
                        side=side,  # type: ignore[arg-type]
                        qty=float(data["qty"]),
                        entry_price=float(data["entry_price"]),
                        entry_ts_ms=int(data["entry_ts_ms"]),
                        stop_loss=float(data["stop_loss"]) if data.get("stop_loss") is not None else None,
                        take_profit=float(data["take_profit"]) if data.get("take_profit") is not None else None,
                        margin_used_usd=0.0,
                        entry_fee_usd=float(data.get("entry_fee_usd", 0.0)),
                    )
            except Exception as exc:
                self.logger.warning(f"[{slot_id}] Corrupt runtime file {path.name} ({exc}); resetting to default")
                atomic_write_json(path, default_symbol_runtime(sym, slot_id))
        return out

    def save_symbol_position(self, slot_id: str, symbol: str, pos: Optional[OpenPosition]) -> None:
        sym = symbol.upper().strip()
        path = self.symbol_path(slot_id, sym)
        if pos is None:
            atomic_write_json(path, default_symbol_runtime(sym, slot_id))
        else:
            atomic_write_json(path, position_to_runtime_dict(pos, slot_id))

    def sync_slot(
        self,
        slot_id: str,
        symbols: Iterable[str],
        positions: Dict[str, OpenPosition],
        start_balance_usd: float,
        realized_balance_usd: float,
        last_funding_bucket: int = -1,
    ) -> None:
        """Полная синхронизация памяти питона -> диск для всех монет слота и _slot_state.json."""
        sym_set = {s.upper().strip() for s in symbols}
        sym_set.update(positions.keys())
        for sym in sorted(sym_set):
            self.save_symbol_position(slot_id, sym, positions.get(sym))
        self.save_slot_meta(slot_id, start_balance_usd, realized_balance_usd, last_funding_bucket)

    def export_backup(self, output_dir: Optional[Path] = None) -> Path:
        """Собирает все файлы рантайма в один читаемый JSON-бэкап (как RuntimeBackupService в HRON_3)."""
        target_dir = Path(output_dir) if output_dir is not None else (self.runtime_dir / "_backups")
        target_dir.mkdir(parents=True, exist_ok=True)

        slots_payload: Dict[str, Dict[str, Any]] = {}
        total_symbols = 0
        if self.runtime_dir.exists():
            for item in sorted(self.runtime_dir.iterdir()):
                if not item.is_dir() or item.name.startswith("_"):
                    continue
                slot_data: Dict[str, Any] = {}
                for jf in sorted(item.glob("*.json")):
                    try:
                        slot_data[jf.stem] = json.loads(jf.read_text(encoding="utf-8"))
                        if not jf.stem.startswith("_"):
                            total_symbols += 1
                    except Exception as exc:
                        self.logger.warning(f"Backup skip unreadable {jf}: {exc}")
                slots_payload[item.name] = slot_data

        now = datetime.datetime.now(datetime.timezone.utc)
        payload = {
            "metadata": {
                "created_at_utc": now.isoformat(),
                "total_slots": len(slots_payload),
                "total_symbols": total_symbols,
                "runtime_dir": str(self.runtime_dir),
            },
            "slots": slots_payload,
        }
        ts_str = now.strftime("%Y%m%d_%H%M%S")
        backup_file = target_dir / f"runtime_backup_{ts_str}.json"
        latest_file = target_dir / "runtime_backup_latest.json"
        atomic_write_json(backup_file, payload)
        atomic_write_json(latest_file, payload)
        return backup_file
