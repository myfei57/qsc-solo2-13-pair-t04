"""磨选组件。

待选渣按「安排给矿 → 加药 → 磨选批次」处理：给矿量与磨矿细度按品位档次安排，
药剂按 kg/t 记录，每个磨选批次落一条完整的金属平衡——给矿量与品位、回收铜量、
铜精矿量与品位、尾渣量与品位，批批可查。回收铜精矿批在并入主精矿流时登记去向，
杜绝「选出来的铜混回精矿里说不清」。
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping

from ..component import Component, ensure_actor
from ..errors import GuardViolation, NotFoundError
from ..machine import StateMachine
from ..ports import SlagYardPort
from ..runtime import RuntimeContext

STATES = ("idle", "feeding", "running")

TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "idle": ("feeding",),
    "feeding": ("running", "idle"),
    "running": ("feeding", "idle"),
}

BATCH_STREAM = "slagmill/batches"
BLEND_STREAM = "slagmill/blends"


class SlagMill(Component):
    name = "slagmill"

    def __init__(self, ctx: RuntimeContext, *, yard: SlagYardPort) -> None:
        super().__init__(ctx)
        self._machine = StateMachine("slagmill", "idle", TRANSITIONS, ctx.clock)
        self._yard = yard
        self._feed_plan: dict[str, Any] | None = None
        self._dosing: dict[str, float] = {}
        self._processed_tons = 0.0
        self._feed_cu_tons = 0.0
        self._recovered_cu_tons = 0.0
        self._concentrate_tons = 0.0
        self._tailings_tons = 0.0
        self._batches_completed = 0
        self._concentrates: dict[str, dict[str, Any]] = {}
        restored = self.restore()
        if restored is not None:
            self._machine.restore(restored)
            plan = restored.get("feed_plan")
            if isinstance(plan, dict):
                self._feed_plan = dict(plan)
            dosing = restored.get("dosing")
            if isinstance(dosing, dict):
                self._dosing = {str(key): float(value) for key, value in dosing.items()}
            self._processed_tons = float(restored.get("processed_tons", 0.0))
            self._feed_cu_tons = float(restored.get("feed_cu_tons", 0.0))
            self._recovered_cu_tons = float(restored.get("recovered_cu_tons", 0.0))
            self._concentrate_tons = float(restored.get("concentrate_tons", 0.0))
            self._tailings_tons = float(restored.get("tailings_tons", 0.0))
            self._batches_completed = int(restored.get("batches_completed", 0))
            concentrates = restored.get("concentrates")
            if isinstance(concentrates, dict):
                self._concentrates = {
                    str(key): dict(value) for key, value in concentrates.items() if isinstance(value, dict)
                }
        self._refresh_gauges()

    # ------------------------------------------------------------------ 动作
    def set_feed(
        self,
        actor: str,
        *,
        rate_tph: float,
        grind_fineness_pct: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "set_feed",
            "slagmill",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if rate_tph <= 0:
                raise GuardViolation("给矿量必须为正", details={"rate_tph": rate_tph})
            if rate_tph > self.settings.slagmill_feed_rate_max_tph + 1e-6:
                raise GuardViolation(
                    "给矿量超过磨机能力上限",
                    details={"rate_tph": rate_tph, "max_tph": self.settings.slagmill_feed_rate_max_tph},
                )
            fineness_min = self.settings.slagmill_grind_fineness_min_pct
            fineness_max = self.settings.slagmill_grind_fineness_max_pct
            if not fineness_min - 1e-6 <= grind_fineness_pct <= fineness_max + 1e-6:
                raise GuardViolation(
                    "磨矿细度超出工艺范围",
                    details={
                        "grind_fineness_pct": grind_fineness_pct,
                        "min_pct": fineness_min,
                        "max_pct": fineness_max,
                    },
                )
            if self._machine.state != "feeding":
                self._machine.to("feeding", actor, "安排给矿")
            self._feed_plan = {
                "rate_tph": round(rate_tph, 3),
                "grind_fineness_pct": round(grind_fineness_pct, 3),
                "set_at": self.clock.timestamp_iso(),
                "actor": actor,
            }
            record = self._persist(reason="set_feed")
            trace.attach(record).note("rate_tph", round(rate_tph, 3)).note(
                "grind_fineness_pct", round(grind_fineness_pct, 3)
            )
            return self.status()

    def dose(
        self,
        actor: str,
        *,
        reagent: str,
        kg_per_t: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "dose",
            f"slagmill/{reagent or 'unassigned'}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require_one_of(("feeding", "running"), "加药")
            if not reagent:
                raise GuardViolation("药剂名不能为空")
            if kg_per_t < 0:
                raise GuardViolation("加药量不能为负", details={"kg_per_t": kg_per_t})
            if kg_per_t > self.settings.slagmill_reagent_max_kgpt + 1e-9:
                raise GuardViolation(
                    "加药量超过上限",
                    details={"kg_per_t": kg_per_t, "max_kgpt": self.settings.slagmill_reagent_max_kgpt},
                )
            if kg_per_t <= 1e-9:
                self._dosing.pop(reagent, None)
            else:
                self._dosing[reagent] = round(kg_per_t, 4)
            record = self._persist(reason="dose")
            trace.attach(record).note("reagent", reagent).note("kg_per_t", round(kg_per_t, 4))
            return self.status()

    def run(
        self,
        actor: str,
        *,
        tons: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "run",
            "slagmill",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require_one_of(("feeding", "running"), "磨选")
            if tons <= 0:
                raise GuardViolation("磨选吨位必须为正", details={"tons": tons})
            plan = self._require_plan()
            active_dosing = {name: dose for name, dose in self._dosing.items() if dose > 0}
            if not active_dosing:
                raise GuardViolation("未加药，禁止磨选", details={"dosing": dict(self._dosing)})
            available = self._yard.feed_available_tons()
            if tons > available + 1e-6:
                raise GuardViolation(
                    "待选渣存量不足，无法按量给矿",
                    details={"requested_tons": tons, "available_tons": available},
                )
            intent = self.write_intent(
                "run",
                {
                    "action": "run",
                    "tons": tons,
                    "rate_tph": plan["rate_tph"],
                    "grind_fineness_pct": plan["grind_fineness_pct"],
                    "dosing": dict(sorted(active_dosing.items())),
                    "at": self.clock.timestamp_iso(),
                    "actor": actor,
                },
            )
            feed = self._yard.consume_feed(tons, actor)
            grade_pct = float(feed["cu_grade_pct"])
            fineness_pct = float(plan["grind_fineness_pct"])
            recovery = self._recovery(grade_pct, fineness_pct)
            feed_cu = float(feed["cu_tons"])
            recovered_cu = feed_cu * recovery
            conc_grade = self.settings.slagmill_conc_grade_pct
            conc_tons = recovered_cu / (conc_grade / 100.0)
            if conc_tons > tons:  # 极端配置兜底：精矿量不可能超过给矿量
                conc_tons = tons
                conc_grade = recovered_cu / tons * 100.0
            tailings_tons = tons - conc_tons
            tailings_cu = feed_cu - recovered_cu
            tailings_grade = tailings_cu / tailings_tons * 100.0 if tailings_tons > 1e-9 else 0.0
            batch_id = f"slagconc-{uuid.uuid4().hex[:12]}"
            produced_at = self.clock.timestamp_iso()
            entry = self.store.append(
                BATCH_STREAM,
                {
                    "batch_id": batch_id,
                    "feed_tons": round(tons, 3),
                    "feed_grade_pct": round(grade_pct, 4),
                    "feed_cu_tons": round(feed_cu, 4),
                    "recovery": round(recovery, 4),
                    "recovered_cu_tons": round(recovered_cu, 4),
                    "concentrate_tons": round(conc_tons, 3),
                    "concentrate_grade_pct": round(conc_grade, 4),
                    "tailings_tons": round(tailings_tons, 3),
                    "tailings_cu_tons": round(tailings_cu, 4),
                    "tailings_grade_pct": round(tailings_grade, 4),
                    "grind_fineness_pct": round(fineness_pct, 3),
                    "rate_tph": plan["rate_tph"],
                    "dosing": dict(sorted(active_dosing.items())),
                    "lots": feed["lots"],
                    "produced_at": produced_at,
                    "actor": actor,
                },
            )
            self._processed_tons = round(self._processed_tons + tons, 3)
            self._feed_cu_tons = round(self._feed_cu_tons + feed_cu, 4)
            self._recovered_cu_tons = round(self._recovered_cu_tons + recovered_cu, 4)
            self._concentrate_tons = round(self._concentrate_tons + conc_tons, 3)
            self._tailings_tons = round(self._tailings_tons + tailings_tons, 3)
            self._batches_completed += 1
            self._concentrates[batch_id] = {
                "batch_id": batch_id,
                "tons": round(conc_tons, 3),
                "cu_tons": round(recovered_cu, 4),
                "grade_pct": round(conc_grade, 4),
                "produced_at": produced_at,
                "blended": False,
                "blended_at": None,
                "blend_target": None,
            }
            if self._machine.state == "feeding":
                self._machine.to("running", actor, f"第 {self._batches_completed} 批磨选")
            self._machine.to("feeding", actor, "批次完成，保持给矿安排")
            record = self._persist(reason="run")
            trace.attach(record).note("batch_id", batch_id).note("batch_seq", entry.seq)
            trace.note("intent_version", intent.version)
            trace.note("recovered_cu_tons", round(recovered_cu, 4)).note(
                "tailings_tons", round(tailings_tons, 3)
            )
            return self.status()

    def blend(
        self,
        actor: str,
        *,
        batch_id: str,
        target: str | None = None,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "blend",
            f"slagmill/{batch_id or 'unassigned'}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            batch = self._concentrates.get(batch_id)
            if batch is None:
                raise NotFoundError("回收铜精矿批不存在", details={"batch_id": batch_id})
            if batch["blended"]:
                raise GuardViolation(
                    "该批回收铜精矿已登记并入，禁止重复",
                    details={"batch_id": batch_id, "blended_at": batch["blended_at"]},
                )
            blend_target = (target or "conc").strip() or "conc"
            batch["blended"] = True
            batch["blended_at"] = self.clock.timestamp_iso()
            batch["blend_target"] = blend_target
            entry = self.store.append(
                BLEND_STREAM,
                {
                    "batch_id": batch_id,
                    "tons": batch["tons"],
                    "cu_tons": batch["cu_tons"],
                    "grade_pct": batch["grade_pct"],
                    "target": blend_target,
                    "blended_at": batch["blended_at"],
                    "actor": actor,
                },
            )
            record = self._persist(reason="blend")
            trace.attach(record).note("blend_seq", entry.seq).note("target", blend_target)
            return self.status()

    def stop(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "stop",
            "slagmill",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require_one_of(("feeding", "running"), "停磨")
            self._machine.to("idle", actor, "停止给矿")
            self._feed_plan = None
            record = self._persist(reason="stop")
            trace.attach(record)
            return self.status()

    # ------------------------------------------------------------------ 查询
    @property
    def state(self) -> str:
        return self._machine.state

    def batches(self, *, limit: int = 20) -> list[Mapping[str, Any]]:
        return [entry.payload for entry in self.store.read_stream(BATCH_STREAM, limit=limit)]

    def blends(self, *, limit: int = 20) -> list[Mapping[str, Any]]:
        return [entry.payload for entry in self.store.read_stream(BLEND_STREAM, limit=limit)]

    def status(self) -> Mapping[str, Any]:
        recovery_rate = (
            round(self._recovered_cu_tons / self._feed_cu_tons, 4) if self._feed_cu_tons > 0 else 0.0
        )
        return {
            "state": self._machine.state,
            "feed_plan": dict(self._feed_plan) if self._feed_plan is not None else None,
            "dosing": dict(sorted(self._dosing.items())),
            "processed_tons": round(self._processed_tons, 3),
            "feed_cu_tons": round(self._feed_cu_tons, 4),
            "recovered_cu_tons": round(self._recovered_cu_tons, 4),
            "recovery_rate": recovery_rate,
            "concentrate_tons": round(self._concentrate_tons, 3),
            "tailings_tons": round(self._tailings_tons, 3),
            "batches_completed": self._batches_completed,
            "unblended_concentrate_tons": round(
                sum(batch["tons"] for batch in self._concentrates.values() if not batch["blended"]), 3
            ),
            "concentrates": [dict(batch) for batch in list(self._concentrates.values())[-8:]],
            "history": list(self._machine.history),
        }

    # ------------------------------------------------------------------ 内部
    def _require_plan(self) -> Mapping[str, Any]:
        if self._feed_plan is None:
            raise GuardViolation("尚未安排给矿，禁止磨选")
        return self._feed_plan

    def _recovery(self, grade_pct: float, fineness_pct: float) -> float:
        """回收率模型：基准回收率随品位与磨矿细度上下浮动，并钳在工艺量程内。"""

        settings = self.settings
        value = (
            settings.slagmill_base_recovery
            + settings.slagmill_recovery_grade_slope * (grade_pct - settings.slagmill_ref_grade_pct)
            + settings.slagmill_recovery_size_slope * (fineness_pct - settings.slagmill_ref_fineness_pct)
        )
        return min(max(value, settings.slagmill_recovery_min), settings.slagmill_recovery_max)

    def _persist(self, *, reason: str) -> Any:
        payload = {
            "state": self._machine.state,
            "reason": reason,
            "written_epoch": self.clock.timestamp(),
            "written_at": self.clock.timestamp_iso(),
            "feed_plan": dict(self._feed_plan) if self._feed_plan is not None else None,
            "dosing": dict(sorted(self._dosing.items())),
            "processed_tons": round(self._processed_tons, 3),
            "feed_cu_tons": round(self._feed_cu_tons, 4),
            "recovered_cu_tons": round(self._recovered_cu_tons, 4),
            "concentrate_tons": round(self._concentrate_tons, 3),
            "tailings_tons": round(self._tailings_tons, 3),
            "batches_completed": self._batches_completed,
            "concentrates": {key: dict(value) for key, value in sorted(self._concentrates.items())},
            "history": list(self._machine.history),
        }
        record = self.persist_state(payload)
        self._refresh_gauges()
        return record

    def _refresh_gauges(self) -> None:
        self.metrics.observe("slagmill.processed_tons", round(self._processed_tons, 3))
        self.metrics.observe("slagmill.recovered_cu_tons", round(self._recovered_cu_tons, 4))
        self.metrics.observe("slagmill.tailings_tons", round(self._tailings_tons, 3))
        self.metrics.observe("slagmill.batches_completed", float(self._batches_completed))


__all__ = ["SlagMill", "STATES", "TRANSITIONS", "BATCH_STREAM", "BLEND_STREAM"]
