"""渣场缓冷组件。

炉渣放出后装包运到渣场，全程按「到货登记 → 排缓冷位 → 化验 → 倒渣入待选堆」
推进：到货必须绑定已放渣的炉次且累计到货不得超过该炉次放渣量；缓冷时长按吨位
计算，时长不到位禁止倒渣；未化验的渣包禁止倒渣，否则进待选堆的渣又成了「品位
没数」的糊涂账。磨选按先到先取从待选堆拿料，每一步都落盘，重启后缓冷计时不丢。
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping

from ..component import Component, ensure_actor
from ..errors import GuardViolation, NotFoundError
from ..machine import StateMachine
from ..ports import SlagPort
from ..runtime import RuntimeContext

STATES = ("empty", "cooling")

TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "empty": ("cooling",),
    "cooling": ("cooling", "empty"),
}

DUMP_STREAM = "slagyard/dumps"
MAX_DUMPED_RECORDS = 50


class SlagYard(Component):
    name = "slagyard"

    def __init__(self, ctx: RuntimeContext, *, slag: SlagPort) -> None:
        super().__init__(ctx)
        self._machine = StateMachine("slagyard", "empty", TRANSITIONS, ctx.clock)
        self._slag = slag
        self._ladles: dict[str, dict[str, Any]] = {}
        self._dumped: list[dict[str, Any]] = []
        self._stock: list[dict[str, Any]] = []
        self._heat_arrivals: dict[str, float] = {}
        self._consumed_tons = 0.0
        restored = self.restore()
        if restored is not None:
            self._machine.restore(restored)
            ladles = restored.get("ladles")
            if isinstance(ladles, dict):
                self._ladles = {
                    str(key): dict(value) for key, value in ladles.items() if isinstance(value, dict)
                }
            dumped = restored.get("dumped")
            if isinstance(dumped, list):
                self._dumped = [dict(entry) for entry in dumped if isinstance(entry, dict)]
            stock = restored.get("stock")
            if isinstance(stock, list):
                self._stock = [dict(entry) for entry in stock if isinstance(entry, dict)]
            arrivals = restored.get("heat_arrivals")
            if isinstance(arrivals, dict):
                self._heat_arrivals = {str(key): float(value) for key, value in arrivals.items()}
            self._consumed_tons = float(restored.get("consumed_tons", 0.0))
        self._refresh_gauges()

    # ------------------------------------------------------------------ 动作
    def arrive(
        self,
        actor: str,
        *,
        heat_id: str,
        ladle_id: str,
        tons: float,
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
            if not heat_id or not ladle_id:
                raise GuardViolation("渣包到货必须绑定炉次号与渣包号")
            if tons <= 0:
                raise GuardViolation("到货吨位必须为正", details={"tons": tons})
            if tons > self.settings.slagyard_ladle_max_tons + 1e-6:
                raise GuardViolation(
                    "到货吨位超过渣包容量上限",
                    details={"tons": tons, "max_tons": self.settings.slagyard_ladle_max_tons},
                )
            tapped = self._slag.require_heat_slagged(heat_id)
            arrived = self._heat_arrivals.get(heat_id, 0.0)
            if arrived + tons > tapped + 1e-6:
                raise GuardViolation(
                    "该炉次累计到货超过放渣量，禁止登记",
                    details={
                        "heat_id": heat_id,
                        "tapped_tons": tapped,
                        "arrived_tons": round(arrived, 3),
                        "requested_tons": tons,
                    },
                )
            if ladle_id in self._ladles:
                raise GuardViolation(
                    "渣包已在渣场，禁止重复到货",
                    details={"ladle_id": ladle_id, "status": self._ladles[ladle_id]["status"]},
                )
            self._ladles[ladle_id] = {
                "ladle_id": ladle_id,
                "heat_id": heat_id,
                "tons": round(tons, 3),
                "status": "arrived",
                "arrived_at": self.clock.timestamp_iso(),
                "cu_grade_pct": None,
                "assayed_at": None,
                "bay_id": None,
                "cooling_started_at": None,
                "cooling_started_epoch": None,
                "required_cool_seconds": 0.0,
            }
            self._heat_arrivals[heat_id] = round(arrived + tons, 3)
            record = self._persist(reason="arrive")
            trace.attach(record).note("heat_id", heat_id).note("tons", round(tons, 3))
            return self.status()

    def assay(
        self,
        actor: str,
        *,
        ladle_id: str,
        cu_grade_pct: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "assay",
            f"slagyard/{ladle_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            ladle = self._require_active_ladle(ladle_id)
            if not 0.0 <= cu_grade_pct <= 100.0:
                raise GuardViolation(
                    "化验品位必须在 0-100% 之间", details={"cu_grade_pct": cu_grade_pct}
                )
            ladle["cu_grade_pct"] = round(cu_grade_pct, 4)
            ladle["assayed_at"] = self.clock.timestamp_iso()
            record = self._persist(reason="assay")
            trace.attach(record).note("cu_grade_pct", round(cu_grade_pct, 4))
            return self.status()

    def schedule_cooling(
        self,
        actor: str,
        *,
        ladle_id: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "schedule_cooling",
            f"slagyard/{ladle_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            ladle = self._require_active_ladle(ladle_id)
            if ladle["status"] != "arrived":
                raise GuardViolation(
                    "渣包当前不在待排冷状态",
                    details={"ladle_id": ladle_id, "status": ladle["status"]},
                )
            bay_id = self._free_bay()
            if bay_id is None:
                raise GuardViolation(
                    "缓冷位已满，倒渣腾空前禁止再排",
                    details={"bays": self._bay_map()},
                )
            required = max(
                self.settings.slagyard_min_cool_seconds,
                float(ladle["tons"]) * self.settings.slagyard_cool_seconds_per_ton,
            )
            ladle["status"] = "cooling"
            ladle["bay_id"] = bay_id
            ladle["cooling_started_epoch"] = self.clock.timestamp()
            ladle["cooling_started_at"] = self.clock.timestamp_iso()
            ladle["required_cool_seconds"] = round(required, 3)
            self._machine.to("cooling", actor, f"渣包 {ladle_id} 入 {bay_id} 缓冷")
            record = self._persist(reason="schedule_cooling")
            trace.attach(record).note("bay_id", bay_id).note(
                "required_cool_seconds", ladle["required_cool_seconds"]
            )
            return self.status()

    def dump(
        self,
        actor: str,
        *,
        ladle_id: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "dump",
            f"slagyard/{ladle_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            ladle = self._require_active_ladle(ladle_id)
            if ladle["status"] != "cooling":
                raise GuardViolation(
                    "渣包未在缓冷，禁止倒渣",
                    details={"ladle_id": ladle_id, "status": ladle["status"]},
                )
            remaining = self._cool_remaining(ladle)
            if remaining > 0:
                raise GuardViolation(
                    "缓冷时长不足，禁止倒渣",
                    details={
                        "ladle_id": ladle_id,
                        "remaining_cool_seconds": round(remaining, 3),
                        "required_cool_seconds": ladle["required_cool_seconds"],
                    },
                )
            if ladle["cu_grade_pct"] is None:
                raise GuardViolation(
                    "渣包未化验，倒渣后品位将无法追溯",
                    details={"ladle_id": ladle_id},
                )
            intent = self.write_intent(
                "dump",
                {
                    "action": "dump",
                    "ladle_id": ladle_id,
                    "bay_id": ladle["bay_id"],
                    "tons": ladle["tons"],
                    "cu_grade_pct": ladle["cu_grade_pct"],
                    "at": self.clock.timestamp_iso(),
                    "actor": actor,
                },
            )
            dumped_at = self.clock.timestamp_iso()
            lot = {
                "lot_id": f"lot-{uuid.uuid4().hex[:12]}",
                "ladle_id": ladle_id,
                "heat_id": ladle["heat_id"],
                "tons": ladle["tons"],
                "remaining_tons": ladle["tons"],
                "cu_grade_pct": ladle["cu_grade_pct"],
                "dumped_at": dumped_at,
            }
            self._stock.append(lot)
            entry = self.store.append(
                DUMP_STREAM,
                {
                    **lot,
                    "bay_id": ladle["bay_id"],
                    "cooled_seconds": round(
                        self.clock.timestamp() - float(ladle["cooling_started_epoch"]), 3
                    ),
                    "actor": actor,
                },
            )
            ladle["status"] = "dumped"
            ladle["dumped_at"] = dumped_at
            del self._ladles[ladle_id]
            self._dumped.append(dict(ladle))
            self._dumped = self._dumped[-MAX_DUMPED_RECORDS:]
            if not any(item["status"] == "cooling" for item in self._ladles.values()):
                self._machine.to("empty", actor, "缓冷位全部腾空")
            record = self._persist(reason="dump")
            trace.attach(record).note("lot_id", lot["lot_id"]).note("dump_seq", entry.seq)
            trace.note("intent_version", intent.version)
            return self.status()

    # ------------------------------------------------------------------ 磨选取料
    def consume_feed(self, tons: float, actor: str) -> Mapping[str, Any]:
        """按先到先取从待选堆扣料，返回加权品位与取料明细。"""

        if tons <= 0:
            raise GuardViolation("给矿吨位必须为正", details={"tons": tons})
        available = self.feed_available_tons()
        if tons > available + 1e-6:
            raise GuardViolation(
                "待选渣存量不足",
                details={"requested_tons": tons, "available_tons": available},
            )
        remaining = tons
        cu_tons = 0.0
        lots_used: list[dict[str, Any]] = []
        for lot in self._stock:
            if remaining <= 1e-9:
                break
            take = min(float(lot["remaining_tons"]), remaining)
            if take <= 0:
                continue
            lot["remaining_tons"] = round(float(lot["remaining_tons"]) - take, 3)
            cu_tons += take * float(lot["cu_grade_pct"]) / 100.0
            lots_used.append(
                {
                    "lot_id": lot["lot_id"],
                    "ladle_id": lot["ladle_id"],
                    "heat_id": lot["heat_id"],
                    "tons": round(take, 3),
                    "cu_grade_pct": lot["cu_grade_pct"],
                }
            )
            remaining -= take
        self._stock = [lot for lot in self._stock if float(lot["remaining_tons"]) > 1e-9]
        self._consumed_tons = round(self._consumed_tons + tons, 3)
        self._persist(reason="consume")
        return {
            "tons": round(tons, 3),
            "cu_tons": round(cu_tons, 4),
            "cu_grade_pct": round(cu_tons / tons * 100.0, 4),
            "lots": lots_used,
        }

    def feed_available_tons(self) -> float:
        return round(sum(float(lot["remaining_tons"]) for lot in self._stock), 3)

    # ------------------------------------------------------------------ 查询
    @property
    def state(self) -> str:
        return self._machine.state

    def status(self) -> Mapping[str, Any]:
        stock_cu = round(
            sum(float(lot["remaining_tons"]) * float(lot["cu_grade_pct"]) / 100.0 for lot in self._stock),
            4,
        )
        bays = self._bay_map()
        return {
            "state": self._machine.state,
            "bays": bays,
            "free_bays": sum(1 for occupant in bays.values() if occupant is None),
            "cooling": self._cooling_report(),
            "ladles": {key: dict(value) for key, value in sorted(self._ladles.items())},
            "dumped_recent": [dict(entry) for entry in self._dumped[-6:]],
            "stock": [dict(lot) for lot in self._stock],
            "stock_tons": self.feed_available_tons(),
            "stock_cu_tons": stock_cu,
            "consumed_tons": round(self._consumed_tons, 3),
            "heat_arrivals": dict(sorted(self._heat_arrivals.items())),
            "history": list(self._machine.history),
        }

    # ------------------------------------------------------------------ 内部
    def _require_active_ladle(self, ladle_id: str) -> dict[str, Any]:
        ladle = self._ladles.get(ladle_id)
        if ladle is None:
            raise NotFoundError(
                "渣包不在渣场（可能未到货或已倒渣）",
                details={"ladle_id": ladle_id},
            )
        return ladle

    def _free_bay(self) -> str | None:
        for bay_id, occupant in self._bay_map().items():
            if occupant is None:
                return bay_id
        return None

    def _bay_map(self) -> dict[str, str | None]:
        bays: dict[str, str | None] = {
            f"BAY-{index}": None for index in range(1, self.settings.slagyard_cooling_bays + 1)
        }
        for ladle in self._ladles.values():
            if ladle["status"] == "cooling" and ladle["bay_id"] in bays:
                bays[ladle["bay_id"]] = ladle["ladle_id"]
        return bays

    def _cool_remaining(self, ladle: Mapping[str, Any]) -> float:
        started = ladle.get("cooling_started_epoch")
        if started is None:
            return float(ladle["required_cool_seconds"])
        elapsed = self.clock.timestamp() - float(started)
        return max(0.0, float(ladle["required_cool_seconds"]) - elapsed)

    def _cooling_report(self) -> list[dict[str, Any]]:
        report: list[dict[str, Any]] = []
        for ladle in self._ladles.values():
            if ladle["status"] != "cooling":
                continue
            remaining = self._cool_remaining(ladle)
            report.append(
                {
                    "ladle_id": ladle["ladle_id"],
                    "bay_id": ladle["bay_id"],
                    "heat_id": ladle["heat_id"],
                    "tons": ladle["tons"],
                    "assayed": ladle["cu_grade_pct"] is not None,
                    "required_cool_seconds": ladle["required_cool_seconds"],
                    "remaining_cool_seconds": round(remaining, 3),
                    "ready_to_dump": remaining <= 0 and ladle["cu_grade_pct"] is not None,
                }
            )
        return sorted(report, key=lambda item: str(item["bay_id"]))

    def _persist(self, *, reason: str) -> Any:
        payload = {
            "state": self._machine.state,
            "reason": reason,
            "written_epoch": self.clock.timestamp(),
            "written_at": self.clock.timestamp_iso(),
            "ladles": {key: dict(value) for key, value in sorted(self._ladles.items())},
            "dumped": [dict(entry) for entry in self._dumped[-MAX_DUMPED_RECORDS:]],
            "stock": [dict(lot) for lot in self._stock],
            "heat_arrivals": dict(sorted(self._heat_arrivals.items())),
            "consumed_tons": round(self._consumed_tons, 3),
            "history": list(self._machine.history),
        }
        record = self.persist_state(payload)
        self._refresh_gauges()
        return record

    def _refresh_gauges(self) -> None:
        self.metrics.observe("slagyard.stock_tons", self.feed_available_tons())
        self.metrics.observe(
            "slagyard.cooling_ladles",
            float(sum(1 for ladle in self._ladles.values() if ladle["status"] == "cooling")),
        )
        self.metrics.observe(
            "slagyard.free_bays",
            float(sum(1 for occupant in self._bay_map().values() if occupant is None)),
        )


__all__ = ["SlagYard", "STATES", "TRANSITIONS", "DUMP_STREAM"]
