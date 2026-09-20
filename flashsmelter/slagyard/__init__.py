"""渣场缓冷组件。

放出的炉渣装包拉到渣场后，在这里登记到货（吨位、渣含铜化验品位）、按到货先后
排缓冷坑位、倒渣入坑计时缓冷，缓冷够时长后倒运成磨选待用渣。每只渣包都与本炉
次的放渣量对账：同一炉次到货累计不得超过已放出的渣量；渣含铜在到货时登记，
后续磨选按批次追溯到炉次与包子号。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..component import Component, ensure_actor
from ..errors import GuardViolation, NotFoundError
from ..machine import StateMachine
from ..ports import SlagPort
from ..runtime import RuntimeContext

CELL_STATES = ("empty", "scheduled", "cooling", "cooled")

CELL_TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "empty": ("scheduled",),
    "scheduled": ("cooling",),
    "cooling": ("cooled",),
    "cooled": ("empty",),
}

ARRIVAL_STREAM = "slagyard/arrivals"
LOT_STREAM = "slagyard/lots"


class _Cell:
    """一个缓冷坑位：状态机 + 当前渣包 + 缓冷计时。"""

    __slots__ = ("cell_id", "machine", "ladle", "poured_at", "cool_seconds")

    def __init__(self, cell_id: str, clock: Any) -> None:
        self.cell_id = cell_id
        self.machine = StateMachine(f"slagyard.{cell_id}", "empty", CELL_TRANSITIONS, clock)
        self.ladle: dict[str, Any] | None = None
        self.poured_at: float | None = None
        self.cool_seconds = 0.0


class SlagYard(Component):
    name = "slagyard"

    def __init__(self, ctx: RuntimeContext, *, slag: SlagPort) -> None:
        super().__init__(ctx)
        self._slag = slag
        self._cells: dict[str, _Cell] = {
            f"cell-{index + 1}": _Cell(f"cell-{index + 1}", ctx.clock)
            for index in range(self.settings.slagyard_cell_count)
        }
        self._queue: list[dict[str, Any]] = []
        self._cooled: list[dict[str, Any]] = []
        self._heat_arrivals: dict[str, float] = {}
        self._ladles_completed = 0
        restored = self.restore()
        if restored is not None:
            queue = restored.get("queue")
            if isinstance(queue, list):
                self._queue = [dict(entry) for entry in queue if isinstance(entry, dict)]
            cells = restored.get("cells")
            if isinstance(cells, dict):
                for cell_id, payload in cells.items():
                    cell = self._cells.get(str(cell_id))
                    if cell is None or not isinstance(payload, Mapping):
                        continue
                    cell.machine.restore(payload)
                    ladle = payload.get("ladle")
                    cell.ladle = dict(ladle) if isinstance(ladle, Mapping) else None
                    cell.poured_at = payload.get("poured_at")
                    cell.cool_seconds = float(payload.get("cool_seconds", 0.0))
            cooled = restored.get("cooled")
            if isinstance(cooled, list):
                self._cooled = [dict(entry) for entry in cooled if isinstance(entry, dict)]
            arrivals = restored.get("heat_arrivals")
            if isinstance(arrivals, Mapping):
                self._heat_arrivals = {str(key): float(value) for key, value in arrivals.items()}
            self._ladles_completed = int(restored.get("ladles_completed", 0))
        self._refresh_gauges()

    # ------------------------------------------------------------------ 动作
    def arrive(
        self,
        actor: str,
        *,
        ladle_id: str,
        heat_id: str,
        tons: float,
        cu_grade: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "arrive",
            f"slagyard/{ladle_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if not ladle_id or not heat_id:
                raise GuardViolation("渣包到货必须给出包子号与炉次号")
            if tons <= 0:
                raise GuardViolation("到货吨位必须为正", details={"tons": tons})
            if tons > self.settings.slagyard_cell_capacity_tons:
                raise GuardViolation(
                    "单包渣量超过缓冷坑位容量",
                    details={"tons": tons, "capacity_tons": self.settings.slagyard_cell_capacity_tons},
                )
            if not 0.0 <= cu_grade <= self.settings.slagyard_grade_max:
                raise GuardViolation(
                    "渣含铜品位超出量程",
                    details={"cu_grade": cu_grade, "max": self.settings.slagyard_grade_max},
                )
            tapped = self._slag.require_heat_slagged(heat_id)
            arrived = self._heat_arrivals.get(heat_id, 0.0)
            if arrived + tons > tapped + 1e-6:
                raise GuardViolation(
                    "本炉次到货累计超过已放出的渣量",
                    details={
                        "heat_id": heat_id,
                        "tapped_tons": tapped,
                        "arrived_tons": round(arrived, 3),
                        "requested_tons": tons,
                    },
                )
            if self._find_ladle(ladle_id) is not None:
                raise GuardViolation(
                    "包子号已在渣场流程中，禁止重复到货", details={"ladle_id": ladle_id}
                )
            entry = {
                "ladle_id": ladle_id,
                "heat_id": heat_id,
                "tons": round(tons, 3),
                "cu_grade": round(cu_grade, 4),
                "arrived_at": self.clock.timestamp_iso(),
                "actor": actor,
            }
            stream_entry = self.store.append(
                ARRIVAL_STREAM,
                {**entry, "heat_arrived_tons": round(arrived + tons, 3), "heat_tapped_tons": tapped},
            )
            self._queue.append(entry)
            self._heat_arrivals[heat_id] = round(arrived + tons, 3)
            record = self._persist(reason="arrive")
            trace.attach(record).note("arrival_seq", stream_entry.seq)
            return self.status()

    def assign(
        self,
        actor: str,
        *,
        cell_id: str | None = None,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "assign",
            "slagyard",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if not self._queue:
                raise NotFoundError("没有等待排缓冷的渣包")
            cell = self._pick_cell(cell_id)
            ladle = self._queue.pop(0)
            cell.ladle = ladle
            cell.machine.to("scheduled", actor, f"渣包 {ladle['ladle_id']} 排入缓冷")
            record = self._persist(reason="assign")
            trace.attach(record).note("ladle_id", ladle["ladle_id"]).note("cell_id", cell.cell_id)
            return self.status()

    def pour(
        self,
        actor: str,
        *,
        cell_id: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "pour",
            f"slagyard/{cell_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            cell = self._require_cell(cell_id)
            cell.machine.require("scheduled", "倒渣")
            ladle = cell.ladle or {}
            intent = self.write_intent(
                "pour",
                {
                    "action": "pour",
                    "cell_id": cell_id,
                    "ladle_id": ladle.get("ladle_id"),
                    "tons": ladle.get("tons"),
                    "at": self.clock.timestamp_iso(),
                    "actor": actor,
                },
            )
            cell.machine.to("cooling", actor, f"渣包 {ladle.get('ladle_id')} 倒渣入坑")
            cell.poured_at = self.clock.timestamp()
            record = self._persist(reason="pour")
            trace.attach(record).note("intent_version", intent.version)
            return self.status()

    def finish_cooling(
        self,
        actor: str,
        *,
        cell_id: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "finish_cooling",
            f"slagyard/{cell_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            cell = self._require_cell(cell_id)
            cell.machine.require("cooling", "缓冷完成判定")
            elapsed = max(0.0, self.clock.timestamp() - float(cell.poured_at or 0.0))
            if elapsed < self.settings.slagyard_min_cool_seconds:
                raise GuardViolation(
                    "缓冷时长不足，禁止翻坑",
                    details={
                        "cell_id": cell_id,
                        "elapsed_seconds": round(elapsed, 3),
                        "required_seconds": self.settings.slagyard_min_cool_seconds,
                    },
                )
            cell.cool_seconds = round(elapsed, 3)
            cell.machine.to("cooled", actor, "缓冷完成")
            record = self._persist(reason="finish_cooling")
            trace.attach(record).note("cool_seconds", cell.cool_seconds)
            return self.status()

    def release(
        self,
        actor: str,
        *,
        cell_id: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "release",
            f"slagyard/{cell_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            cell = self._require_cell(cell_id)
            cell.machine.require("cooled", "倒运入磨")
            ladle = cell.ladle or {}
            lot = {
                "lot_id": f"lot-{self._ladles_completed + 1:05d}",
                "ladle_id": ladle.get("ladle_id"),
                "heat_id": ladle.get("heat_id"),
                "tons": ladle.get("tons"),
                "cu_grade": ladle.get("cu_grade"),
                "cool_seconds": cell.cool_seconds,
                "released_at": self.clock.timestamp_iso(),
                "actor": actor,
            }
            stream_entry = self.store.append(LOT_STREAM, lot)
            self._cooled.append(lot)
            self._ladles_completed += 1
            cell.ladle = None
            cell.poured_at = None
            cell.cool_seconds = 0.0
            cell.machine.to("empty", actor, "坑位倒空")
            record = self._persist(reason="release")
            trace.attach(record).note("lot_id", lot["lot_id"]).note("lot_seq", stream_entry.seq)
            return self.status()

    # ------------------------------------------------------------------ 查询
    def cooled_stock_tons(self) -> float:
        return round(sum(float(lot["tons"]) for lot in self._cooled), 3)

    def peek_cooled(self, tons: float) -> list[Mapping[str, Any]]:
        """按 FIFO 预览取用 ``tons`` 缓冷渣会取到哪些批次，不改变库存。"""

        return self._slice_cooled(tons)

    def consume_cooled(self, tons: float, actor: str) -> list[Mapping[str, Any]]:
        """磨选按 FIFO 取走缓冷渣；返回取走的批次切片（含品位，供加权计算）。"""

        slices = self._slice_cooled(tons)
        remaining = tons
        while remaining > 1e-9 and self._cooled:
            lot = self._cooled[0]
            take = min(float(lot["tons"]), remaining)
            remaining = round(remaining - take, 9)
            if take >= float(lot["tons"]) - 1e-9:
                self._cooled.pop(0)
            else:
                lot["tons"] = round(float(lot["tons"]) - take, 3)
        self._persist(reason="consume_cooled")
        self._refresh_gauges()
        return slices

    def arrivals(self, *, limit: int = 50) -> list[Mapping[str, Any]]:
        return [entry.payload for entry in self.store.read_stream(ARRIVAL_STREAM, limit=limit)]

    def lots(self, *, limit: int = 50) -> list[Mapping[str, Any]]:
        return [entry.payload for entry in self.store.read_stream(LOT_STREAM, limit=limit)]

    def status(self) -> Mapping[str, Any]:
        return {
            "state": self._yard_state(),
            "queue": [dict(entry) for entry in self._queue],
            "queue_length": len(self._queue),
            "cells": {cell_id: self._cell_status(cell) for cell_id, cell in sorted(self._cells.items())},
            "cells_free": sum(1 for cell in self._cells.values() if cell.machine.state == "empty"),
            "cell_count": len(self._cells),
            "cooled_stock": [dict(lot) for lot in self._cooled],
            "cooled_stock_tons": self.cooled_stock_tons(),
            "heat_arrivals": dict(sorted(self._heat_arrivals.items())),
            "ladles_completed": self._ladles_completed,
            "min_cool_seconds": self.settings.slagyard_min_cool_seconds,
        }

    # ------------------------------------------------------------------ 内部
    def _slice_cooled(self, tons: float) -> list[dict[str, Any]]:
        if tons <= 0:
            raise GuardViolation("取用吨位必须为正", details={"tons": tons})
        if tons > self.cooled_stock_tons() + 1e-6:
            raise GuardViolation(
                "缓冷渣库存不足",
                details={"requested_tons": tons, "available_tons": self.cooled_stock_tons()},
            )
        slices: list[dict[str, Any]] = []
        remaining = tons
        for lot in self._cooled:
            if remaining <= 1e-9:
                break
            take = min(float(lot["tons"]), remaining)
            remaining = round(remaining - take, 9)
            slices.append(
                {
                    "lot_id": lot["lot_id"],
                    "ladle_id": lot["ladle_id"],
                    "heat_id": lot["heat_id"],
                    "tons": round(take, 3),
                    "cu_grade": lot["cu_grade"],
                    "cool_seconds": lot["cool_seconds"],
                }
            )
        return slices

    def _yard_state(self) -> str:
        """汇总状态：渣场没有单一状态机，按坑位与队列推导一个供总览视图使用。"""

        states = {cell.machine.state for cell in self._cells.values()}
        for candidate in ("cooling", "scheduled", "cooled"):
            if candidate in states:
                return candidate
        return "queued" if self._queue else "empty"

    def _pick_cell(self, cell_id: str | None) -> _Cell:
        if cell_id:
            cell = self._cells.get(cell_id)
            if cell is None:
                raise NotFoundError("缓冷坑位不存在", details={"cell_id": cell_id})
            if cell.machine.state != "empty":
                raise GuardViolation(
                    "指定坑位被占用",
                    details={"cell_id": cell_id, "state": cell.machine.state},
                )
            return cell
        for cell in self._cells.values():
            if cell.machine.state == "empty":
                return cell
        raise GuardViolation(
            "缓冷坑位已全部占用，无法排缓冷",
            details={"cells": {cell_id: cell.machine.state for cell_id, cell in self._cells.items()}},
        )

    def _require_cell(self, cell_id: str) -> _Cell:
        cell = self._cells.get(cell_id or "")
        if cell is None:
            raise NotFoundError("缓冷坑位不存在", details={"cell_id": cell_id})
        return cell

    def _find_ladle(self, ladle_id: str) -> Mapping[str, Any] | None:
        for entry in self._queue:
            if entry["ladle_id"] == ladle_id:
                return entry
        for cell in self._cells.values():
            if cell.ladle is not None and cell.ladle.get("ladle_id") == ladle_id:
                return cell.ladle
        for lot in self._cooled:
            if lot["ladle_id"] == ladle_id:
                return lot
        return None

    def _cell_status(self, cell: _Cell) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "state": cell.machine.state,
            "ladle": dict(cell.ladle) if cell.ladle is not None else None,
            "cool_seconds": round(cell.cool_seconds, 3),
            "history": list(cell.machine.history),
        }
        if cell.machine.state == "cooling" and cell.poured_at is not None:
            elapsed = max(0.0, self.clock.timestamp() - float(cell.poured_at))
            payload["cooling_elapsed_seconds"] = round(elapsed, 3)
            payload["cooling_remaining_seconds"] = round(
                max(0.0, self.settings.slagyard_min_cool_seconds - elapsed), 3
            )
        return payload

    def _persist(self, *, reason: str) -> Any:
        payload = {
            "reason": reason,
            "written_epoch": self.clock.timestamp(),
            "written_at": self.clock.timestamp_iso(),
            "queue": [dict(entry) for entry in self._queue],
            "cells": {
                cell_id: {
                    "state": cell.machine.state,
                    "ladle": dict(cell.ladle) if cell.ladle is not None else None,
                    "poured_at": cell.poured_at,
                    "cool_seconds": round(cell.cool_seconds, 3),
                    "history": list(cell.machine.history),
                }
                for cell_id, cell in sorted(self._cells.items())
            },
            "cooled": [dict(lot) for lot in self._cooled],
            "heat_arrivals": dict(sorted(self._heat_arrivals.items())),
            "ladles_completed": self._ladles_completed,
        }
        record = self.persist_state(payload)
        self._refresh_gauges()
        return record

    def _refresh_gauges(self) -> None:
        self.metrics.observe("slagyard.queue_length", float(len(self._queue)))
        self.metrics.observe("slagyard.cooled_stock_tons", self.cooled_stock_tons())
        self.metrics.observe(
            "slagyard.cells_free", float(sum(1 for cell in self._cells.values() if cell.machine.state == "empty"))
        )
        self.metrics.observe("slagyard.ladles_completed", float(self._ladles_completed))


__all__ = ["SlagYard", "CELL_STATES", "CELL_TRANSITIONS", "ARRIVAL_STREAM", "LOT_STREAM"]
