"""磨选组件。

缓冷后的炉渣在这里磨细浮选：给矿按缓冷渣库存与品位排计划，捕收剂、起泡剂按
品位配方核算并容差校验，磨矿粒度决定回收率。每批都记清给矿量、选出铜量与剩余
尾渣量；铜精矿批记录带来源炉次与包子号，回掺精矿时多少铜、从哪来都查得到。
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping

from ..component import Component, ensure_actor
from ..errors import GuardViolation
from ..machine import StateMachine
from ..ports import SlagYardPort
from ..runtime import RuntimeContext

STATES = ("idle", "scheduled", "dosed", "running")

TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "idle": ("scheduled",),
    "scheduled": ("dosed",),
    "dosed": ("running",),
    "running": ("idle",),
}

BATCH_STREAM = "mill/batches"


class Mill(Component):
    name = "mill"

    def __init__(self, ctx: RuntimeContext, *, yard: SlagYardPort) -> None:
        super().__init__(ctx)
        self._machine = StateMachine("mill", "idle", TRANSITIONS, ctx.clock)
        self._yard = yard
        self._plan: dict[str, Any] | None = None
        self._dose: dict[str, Any] | None = None
        self._result: dict[str, Any] | None = None
        self._total_feed_tons = 0.0
        self._total_conc_tons = 0.0
        self._total_cu_recovered_tons = 0.0
        self._total_tail_tons = 0.0
        self._batches_completed = 0
        restored = self.restore()
        if restored is not None:
            self._machine.restore(restored)
            plan = restored.get("plan")
            self._plan = dict(plan) if isinstance(plan, Mapping) else None
            dose = restored.get("dose")
            self._dose = dict(dose) if isinstance(dose, Mapping) else None
            result = restored.get("result")
            self._result = dict(result) if isinstance(result, Mapping) else None
            self._total_feed_tons = float(restored.get("total_feed_tons", 0.0))
            self._total_conc_tons = float(restored.get("total_conc_tons", 0.0))
            self._total_cu_recovered_tons = float(restored.get("total_cu_recovered_tons", 0.0))
            self._total_tail_tons = float(restored.get("total_tail_tons", 0.0))
            self._batches_completed = int(restored.get("batches_completed", 0))
        self._refresh_gauges()

    # ------------------------------------------------------------------ 动作
    def schedule(
        self,
        actor: str,
        *,
        tons: float,
        rate_tph: float,
        grind_um: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "schedule",
            "mill",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require("idle", "安排给矿")
            if tons <= 0:
                raise GuardViolation("给矿量必须为正", details={"tons": tons})
            if not 0 < rate_tph <= self.settings.mill_feed_rate_max_tph:
                raise GuardViolation(
                    "给矿速率超出量程",
                    details={"rate_tph": rate_tph, "max_tph": self.settings.mill_feed_rate_max_tph},
                )
            if not self.settings.mill_grind_min_um <= grind_um <= self.settings.mill_grind_max_um:
                raise GuardViolation(
                    "磨矿粒度超出量程",
                    details={
                        "grind_um": grind_um,
                        "min_um": self.settings.mill_grind_min_um,
                        "max_um": self.settings.mill_grind_max_um,
                    },
                )
            preview = self._yard.peek_cooled(tons)
            feed_grade = sum(float(lot["tons"]) * float(lot["cu_grade"]) for lot in preview) / tons
            if feed_grade >= self.settings.mill_conc_grade:
                raise GuardViolation(
                    "给矿品位不低于精矿品位，无法分选",
                    details={"feed_grade": round(feed_grade, 4), "conc_grade": self.settings.mill_conc_grade},
                )
            lots = [dict(lot) for lot in self._yard.consume_cooled(tons, actor)]
            grade_pct = feed_grade * 100.0
            collector = self.settings.mill_collector_base_gpt + self.settings.mill_collector_per_percent_gpt * grade_pct
            frother = self.settings.mill_frother_base_gpt + self.settings.mill_frother_per_percent_gpt * grade_pct
            batch_id = f"mill-{uuid.uuid4().hex[:12]}"
            self._plan = {
                "batch_id": batch_id,
                "lots": lots,
                "tons": round(tons, 3),
                "feed_grade": round(feed_grade, 4),
                "rate_tph": rate_tph,
                "grind_um": grind_um,
                "collector_gpt": round(collector, 2),
                "frother_gpt": round(frother, 2),
                "run_seconds": round(tons / rate_tph * 3600.0, 3),
                "scheduled_at": self.clock.timestamp_iso(),
            }
            self._machine.to("scheduled", actor, f"批次 {batch_id} 给矿计划")
            record = self._persist(reason="schedule")
            trace.attach(record).note("batch_id", batch_id).note("feed_grade", round(feed_grade, 4))
            return self.status()

    def dose(
        self,
        actor: str,
        *,
        collector_gpt: float,
        frother_gpt: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "dose",
            f"mill/{self._batch_tag()}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require("scheduled", "加药确认")
            plan = self._require_plan()
            tolerance = self.settings.mill_reagent_tolerance
            for name, actual, planned in (
                ("collector_gpt", collector_gpt, float(plan["collector_gpt"])),
                ("frother_gpt", frother_gpt, float(plan["frother_gpt"])),
            ):
                if actual <= 0:
                    raise GuardViolation("加药量必须为正", details={"reagent": name, "actual": actual})
                if abs(actual - planned) > tolerance * planned + 1e-9:
                    raise GuardViolation(
                        "加药量偏离按品位核算的计划",
                        details={
                            "reagent": name,
                            "actual_gpt": actual,
                            "planned_gpt": planned,
                            "tolerance": tolerance,
                        },
                    )
            self._dose = {
                "collector_gpt": collector_gpt,
                "frother_gpt": frother_gpt,
                "dosed_at": self.clock.timestamp_iso(),
            }
            self._machine.to("dosed", actor, "加药确认")
            record = self._persist(reason="dose")
            trace.attach(record).note("collector_gpt", collector_gpt).note("frother_gpt", frother_gpt)
            return self.status()

    def run(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "run",
            f"mill/{self._batch_tag()}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require("dosed", "磨选运行")
            plan = self._require_plan()
            settings = self.settings
            recovery = settings.mill_recovery_base - settings.mill_recovery_grind_coeff * (
                float(plan["grind_um"]) - settings.mill_recovery_reference_um
            )
            recovery = min(max(recovery, settings.mill_recovery_min), settings.mill_recovery_max)
            tons = float(plan["tons"])
            grade = float(plan["feed_grade"])
            cu_in = tons * grade
            cu_recovered = cu_in * recovery
            conc_tons = cu_recovered / settings.mill_conc_grade
            tail_tons = tons - conc_tons
            tail_cu = cu_in - cu_recovered
            intent = self.write_intent(
                "run",
                {
                    "action": "run",
                    "batch_id": plan["batch_id"],
                    "tons": tons,
                    "grind_um": plan["grind_um"],
                    "at": self.clock.timestamp_iso(),
                    "actor": actor,
                },
            )
            self._result = {
                "recovery": round(recovery, 4),
                "cu_in_tons": round(cu_in, 4),
                "cu_recovered_tons": round(cu_recovered, 4),
                "conc_tons": round(conc_tons, 3),
                "conc_grade": settings.mill_conc_grade,
                "tail_tons": round(tail_tons, 3),
                "tail_cu_tons": round(tail_cu, 4),
                "tail_grade": round(tail_cu / tail_tons, 4) if tail_tons > 1e-9 else 0.0,
            }
            self._machine.to("running", actor, "磨选运行")
            record = self._persist(reason="run")
            trace.attach(record).note("intent_version", intent.version).note(
                "recovery", self._result["recovery"]
            )
            return self.status()

    def finish_batch(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "finish_batch",
            f"mill/{self._batch_tag()}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require("running", "结束磨选批次")
            plan = self._require_plan()
            if self._result is None:
                raise GuardViolation("磨选尚未运行，不能结束批次", details={"state": self._machine.state})
            dose = self._dose or {}
            result = self._result
            heat_ids = sorted({str(lot["heat_id"]) for lot in plan["lots"]})
            entry = self.store.append(
                BATCH_STREAM,
                {
                    "batch_id": plan["batch_id"],
                    "heat_ids": heat_ids,
                    "ladle_ids": [lot["ladle_id"] for lot in plan["lots"]],
                    "feed_tons": plan["tons"],
                    "feed_grade": plan["feed_grade"],
                    "rate_tph": plan["rate_tph"],
                    "grind_um": plan["grind_um"],
                    "collector_gpt": dose.get("collector_gpt"),
                    "frother_gpt": dose.get("frother_gpt"),
                    **result,
                    "run_seconds": plan["run_seconds"],
                    "completed_at": self.clock.timestamp_iso(),
                    "actor": actor,
                },
            )
            self._total_feed_tons = round(self._total_feed_tons + float(plan["tons"]), 3)
            self._total_conc_tons = round(self._total_conc_tons + float(result["conc_tons"]), 3)
            self._total_cu_recovered_tons = round(
                self._total_cu_recovered_tons + float(result["cu_recovered_tons"]), 4
            )
            self._total_tail_tons = round(self._total_tail_tons + float(result["tail_tons"]), 3)
            self._batches_completed += 1
            self._plan = None
            self._dose = None
            self._result = None
            self._machine.to("idle", actor, f"第 {self._batches_completed} 批磨选结束")
            record = self._persist(reason="finish_batch")
            trace.attach(record).note("batch_seq", entry.seq).note(
                "batches_completed", self._batches_completed
            )
            return self.status()

    # ------------------------------------------------------------------ 查询
    @property
    def state(self) -> str:
        return self._machine.state

    def totals(self) -> Mapping[str, Any]:
        """累计账：处理多少渣、选出多少铜、剩多少尾渣。"""

        return {
            "feed_tons": round(self._total_feed_tons, 3),
            "conc_tons": round(self._total_conc_tons, 3),
            "cu_recovered_tons": round(self._total_cu_recovered_tons, 4),
            "tail_tons": round(self._total_tail_tons, 3),
        }

    def batches(self, *, limit: int = 20) -> list[Mapping[str, Any]]:
        return [entry.payload for entry in self.store.read_stream(BATCH_STREAM, limit=limit)]

    def status(self) -> Mapping[str, Any]:
        return {
            "state": self._machine.state,
            "plan": dict(self._plan) if self._plan is not None else None,
            "dose": dict(self._dose) if self._dose is not None else None,
            "result": dict(self._result) if self._result is not None else None,
            "totals": dict(self.totals()),
            "batches_completed": self._batches_completed,
            "cooled_stock_tons": self._yard.cooled_stock_tons(),
            "limits": {
                "feed_rate_max_tph": self.settings.mill_feed_rate_max_tph,
                "grind_min_um": self.settings.mill_grind_min_um,
                "grind_max_um": self.settings.mill_grind_max_um,
                "reagent_tolerance": self.settings.mill_reagent_tolerance,
                "conc_grade": self.settings.mill_conc_grade,
            },
            "history": list(self._machine.history),
        }

    # ------------------------------------------------------------------ 内部
    def _batch_tag(self) -> str:
        if self._plan is None:
            return "unassigned"
        return str(self._plan["batch_id"])

    def _require_plan(self) -> Mapping[str, Any]:
        if self._plan is None:
            raise GuardViolation("当前没有给矿计划", details={"state": self._machine.state})
        return self._plan

    def _persist(self, *, reason: str) -> Any:
        payload = {
            "state": self._machine.state,
            "reason": reason,
            "written_epoch": self.clock.timestamp(),
            "written_at": self.clock.timestamp_iso(),
            "plan": self._plan,
            "dose": self._dose,
            "result": self._result,
            "total_feed_tons": round(self._total_feed_tons, 3),
            "total_conc_tons": round(self._total_conc_tons, 3),
            "total_cu_recovered_tons": round(self._total_cu_recovered_tons, 4),
            "total_tail_tons": round(self._total_tail_tons, 3),
            "batches_completed": self._batches_completed,
            "history": list(self._machine.history),
        }
        record = self.persist_state(payload)
        self._refresh_gauges()
        return record

    def _refresh_gauges(self) -> None:
        self.metrics.observe("mill.feed_tons_total", round(self._total_feed_tons, 3))
        self.metrics.observe("mill.cu_recovered_tons_total", round(self._total_cu_recovered_tons, 4))
        self.metrics.observe("mill.tail_tons_total", round(self._total_tail_tons, 3))
        self.metrics.observe("mill.batches_completed", float(self._batches_completed))


__all__ = ["Mill", "STATES", "TRANSITIONS", "BATCH_STREAM"]
