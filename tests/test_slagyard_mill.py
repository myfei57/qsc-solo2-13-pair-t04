"""渣场缓冷与磨选选铜：到货对账、缓冷排队、给矿加药与批次台账。"""

from __future__ import annotations

import unittest

from flashsmelter.errors import GuardViolation, NotFoundError, StateTransitionError
from flashsmelter.runtime import ManualClock

from .helpers import feed_heat, make_app, make_root, start_furnace

# 沉淀池单炉次可放渣约 17.55t（0.15m × 45m² × 2.6t/m³），放渣链路按两次扣减
# 记账（tap 与 end_tap 各扣一次），因此单炉次放渣目标量取 8t 以内。
TAP_TONS = 8.0


def tap_slag(app, heat_id: str, slag_tons: float = TAP_TONS, ladle_id: str | None = None):
    """跑一炉并放出指定吨位的渣，让渣场到货能对上账。"""

    if app.furnace.state == "cold":
        start_furnace(app)
    # 缓冷会把时钟推进十几小时，下一炉次喷吹前必须刷新分析仪基线。
    app.oxygen.set_baseline("tester", value=0.62, source="analyzer-a")
    feed_heat(app, heat_id)
    return app.furnace.tap(
        "tester",
        heat_id=heat_id,
        ladle_id=ladle_id or f"L-{heat_id}",
        slag_tons=slag_tons,
        matte_tons=30.0,
    )


def arrive(app, ladle_id: str, heat_id: str = "H-1", tons: float = 6.0, cu_grade: float = 0.012):
    return app.slagyard.arrive("yard", ladle_id=ladle_id, heat_id=heat_id, tons=tons, cu_grade=cu_grade)


def cool_one_ladle(app, ladle_id: str, heat_id: str = "H-1", tons: float = 6.0, cu_grade: float = 0.012):
    """到货 → 排缓冷 → 倒渣 → 缓冷够时长 → 倒运入磨，返回坑位号。"""

    arrive(app, ladle_id, heat_id, tons, cu_grade)
    status = app.slagyard.assign("yard")
    cell_id = next(
        cid for cid, cell in status["cells"].items() if cell["ladle"] and cell["ladle"]["ladle_id"] == ladle_id
    )
    app.slagyard.pour("yard", cell_id=cell_id)
    app.clock.advance(app.settings.slagyard_min_cool_seconds + 1)
    app.slagyard.finish_cooling("yard", cell_id=cell_id)
    app.slagyard.release("yard", cell_id=cell_id)
    return cell_id


class SlagYardArrivalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()

    def test_arrival_requires_tapped_heat(self) -> None:
        with self.assertRaises(NotFoundError) as blocked:
            arrive(self.app, "SL-1", "H-9")
        self.assertEqual("not-found", blocked.exception.code)

    def test_arrival_reconciles_against_tapped_tons(self) -> None:
        tap_slag(self.app, "H-1")
        arrive(self.app, "SL-1", tons=5.0)
        with self.assertRaises(GuardViolation) as blocked:
            arrive(self.app, "SL-2", tons=4.0)
        self.assertEqual(TAP_TONS, blocked.exception.details["tapped_tons"])
        self.assertEqual(5.0, blocked.exception.details["arrived_tons"])
        arrive(self.app, "SL-2", tons=3.0)
        self.assertEqual({"H-1": TAP_TONS}, self.app.slagyard.status()["heat_arrivals"])

    def test_arrival_validates_payload(self) -> None:
        tap_slag(self.app, "H-1")
        with self.assertRaises(GuardViolation):
            arrive(self.app, "SL-1", tons=0.0)
        with self.assertRaises(GuardViolation):
            arrive(self.app, "SL-1", tons=self.app.settings.slagyard_cell_capacity_tons + 1.0)
        with self.assertRaises(GuardViolation):
            arrive(self.app, "SL-1", cu_grade=self.app.settings.slagyard_grade_max + 0.01)
        arrive(self.app, "SL-1", tons=6.0)
        with self.assertRaises(GuardViolation):
            arrive(self.app, "SL-1", tons=2.0)  # 同一包子重复到货

    def test_arrival_is_audited_and_streamed(self) -> None:
        tap_slag(self.app, "H-1")
        arrive(self.app, "SL-1", tons=6.0, cu_grade=0.011)
        arrivals = self.app.slagyard.arrivals()
        self.assertEqual(1, len(arrivals))
        self.assertEqual("SL-1", arrivals[0]["ladle_id"])
        self.assertEqual(0.011, arrivals[0]["cu_grade"])
        self.assertEqual(TAP_TONS, arrivals[0]["heat_tapped_tons"])


class SlagYardSchedulingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app(slagyard_cell_count=2)
        tap_slag(self.app, "H-1")

    def test_assign_is_fifo_by_arrival(self) -> None:
        arrive(self.app, "SL-1", tons=4.0)
        arrive(self.app, "SL-2", tons=4.0)
        status = self.app.slagyard.assign("yard")
        self.assertEqual("SL-1", status["cells"]["cell-1"]["ladle"]["ladle_id"])
        status = self.app.slagyard.assign("yard")
        self.assertEqual("SL-2", status["cells"]["cell-2"]["ladle"]["ladle_id"])
        self.assertEqual(0, status["queue_length"])

    def test_assign_needs_queue_and_free_cell(self) -> None:
        with self.assertRaises(NotFoundError):
            self.app.slagyard.assign("yard")
        arrive(self.app, "SL-1", tons=3.0)
        arrive(self.app, "SL-2", tons=3.0)
        tap_slag(self.app, "H-2")
        arrive(self.app, "SL-3", "H-2", tons=3.0)
        self.app.slagyard.assign("yard")
        self.app.slagyard.assign("yard")
        with self.assertRaises(GuardViolation) as blocked:
            self.app.slagyard.assign("yard")
        self.assertIn("cell-1", blocked.exception.details["cells"])
        with self.assertRaises(NotFoundError):
            self.app.slagyard.assign("yard", cell_id="cell-9")
        with self.assertRaises(GuardViolation):
            self.app.slagyard.assign("yard", cell_id="cell-1")

    def test_pour_and_cooling_time_enforced(self) -> None:
        with self.assertRaises(NotFoundError):
            self.app.slagyard.pour("yard", cell_id="cell-9")
        with self.assertRaises(StateTransitionError):
            self.app.slagyard.pour("yard", cell_id="cell-1")  # 坑位空着，未排缓冷
        arrive(self.app, "SL-1", tons=4.0)
        self.app.slagyard.assign("yard")
        with self.assertRaises(StateTransitionError):
            self.app.slagyard.finish_cooling("yard", cell_id="cell-1")
        self.app.slagyard.pour("yard", cell_id="cell-1")
        with self.assertRaises(GuardViolation) as blocked:
            self.app.slagyard.finish_cooling("yard", cell_id="cell-1")
        self.assertLess(blocked.exception.details["elapsed_seconds"], blocked.exception.details["required_seconds"])
        self.app.clock.advance(self.app.settings.slagyard_min_cool_seconds + 1)
        status = self.app.slagyard.finish_cooling("yard", cell_id="cell-1")
        self.assertEqual("cooled", status["cells"]["cell-1"]["state"])
        self.assertGreater(status["cells"]["cell-1"]["cool_seconds"], 0.0)

    def test_release_builds_cooled_stock(self) -> None:
        cool_one_ladle(self.app, "SL-1", tons=5.0, cu_grade=0.013)
        status = self.app.slagyard.status()
        self.assertEqual("empty", status["cells"]["cell-1"]["state"])
        self.assertEqual(5.0, status["cooled_stock_tons"])
        self.assertEqual(1, status["ladles_completed"])
        lots = self.app.slagyard.lots()
        self.assertEqual(1, len(lots))
        self.assertEqual("H-1", lots[0]["heat_id"])
        self.assertEqual("SL-1", lots[0]["ladle_id"])
        self.assertEqual(0.013, lots[0]["cu_grade"])

    def test_state_survives_restart(self) -> None:
        root = make_root()
        clock = ManualClock()
        app = make_app(root=root, clock=clock, slagyard_cell_count=2)
        tap_slag(app, "H-1")
        arrive(app, "SL-1", tons=6.0)
        app.slagyard.assign("yard")
        app.slagyard.pour("yard", cell_id="cell-1")
        reopened = make_app(root=root, clock=clock, slagyard_cell_count=2)
        status = reopened.slagyard.status()
        self.assertEqual("cooling", status["cells"]["cell-1"]["state"])
        self.assertEqual("SL-1", status["cells"]["cell-1"]["ladle"]["ladle_id"])
        self.assertEqual({"H-1": 6.0}, status["heat_arrivals"])
        clock.advance(reopened.settings.slagyard_min_cool_seconds + 1)
        reopened.slagyard.finish_cooling("yard", cell_id="cell-1")
        status = reopened.slagyard.release("yard", cell_id="cell-1")
        self.assertEqual(6.0, status["cooled_stock_tons"])


class MillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()

    def _stock(self, tons: float = 6.0, cu_grade: float = 0.012, ladle_id: str = "SL-1") -> None:
        tap_slag(self.app, "H-1")
        cool_one_ladle(self.app, ladle_id, tons=tons, cu_grade=cu_grade)

    def test_schedule_guards(self) -> None:
        with self.assertRaises(GuardViolation):
            self.app.mill.schedule("mill", tons=1.0, rate_tph=100.0, grind_um=75.0)  # 无库存
        self._stock()
        with self.assertRaises(GuardViolation):
            self.app.mill.schedule("mill", tons=1.0, rate_tph=999.0, grind_um=75.0)  # 速率超限
        with self.assertRaises(GuardViolation):
            self.app.mill.schedule("mill", tons=1.0, rate_tph=100.0, grind_um=10.0)  # 粒度超限
        with self.assertRaises(GuardViolation):
            self.app.mill.schedule("mill", tons=99.0, rate_tph=100.0, grind_um=75.0)  # 超库存
        with self.assertRaises(GuardViolation):
            self.app.mill.schedule("mill", tons=0.0, rate_tph=100.0, grind_um=75.0)

    def test_schedule_computes_grade_based_reagent_plan(self) -> None:
        self._stock(tons=6.0, cu_grade=0.01)
        status = self.app.mill.schedule("mill", tons=6.0, rate_tph=100.0, grind_um=75.0)
        plan = status["plan"]
        self.assertEqual(0.01, plan["feed_grade"])
        self.assertEqual(35.0, plan["collector_gpt"])  # 20 + 15 × 1.0%Cu
        self.assertEqual(12.0, plan["frother_gpt"])  # 10 + 2 × 1.0%Cu
        self.assertEqual(0.0, status["cooled_stock_tons"])

    def test_dose_must_follow_grade_based_plan(self) -> None:
        self._stock(tons=6.0, cu_grade=0.01)
        self.app.mill.schedule("mill", tons=6.0, rate_tph=100.0, grind_um=75.0)
        with self.assertRaises(GuardViolation) as blocked:
            self.app.mill.dose("mill", collector_gpt=50.0, frother_gpt=12.0)
        self.assertEqual(35.0, blocked.exception.details["planned_gpt"])
        with self.assertRaises(GuardViolation):
            self.app.mill.dose("mill", collector_gpt=35.0, frother_gpt=0.0)
        status = self.app.mill.dose("mill", collector_gpt=35.0, frother_gpt=12.0)
        self.assertEqual("dosed", status["state"])

    def test_sequence_is_enforced(self) -> None:
        self._stock()
        with self.assertRaises(StateTransitionError):
            self.app.mill.dose("mill", collector_gpt=35.0, frother_gpt=12.0)
        with self.assertRaises(StateTransitionError):
            self.app.mill.run("mill")
        self.app.mill.schedule("mill", tons=6.0, rate_tph=100.0, grind_um=75.0)
        with self.assertRaises(StateTransitionError):
            self.app.mill.run("mill")
        with self.assertRaises(StateTransitionError):
            self.app.mill.finish_batch("mill")

    def test_full_chain_mass_balance_and_traceability(self) -> None:
        tap_slag(self.app, "H-1")
        cool_one_ladle(self.app, "SL-1", "H-1", tons=6.0, cu_grade=0.012)
        tap_slag(self.app, "H-2")
        cool_one_ladle(self.app, "SL-2", "H-2", tons=6.0, cu_grade=0.008)
        status = self.app.mill.schedule("mill", tons=12.0, rate_tph=100.0, grind_um=75.0)
        self.assertEqual(0.01, status["plan"]["feed_grade"])
        self.app.mill.dose("mill", collector_gpt=35.0, frother_gpt=12.0)
        status = self.app.mill.run("mill")
        result = status["result"]
        self.assertEqual(0.88, result["recovery"])
        self.assertEqual(0.12, result["cu_in_tons"])
        self.assertEqual(0.1056, result["cu_recovered_tons"])
        self.assertAlmostEqual(12.0, result["conc_tons"] + result["tail_tons"], places=6)
        self.assertAlmostEqual(0.12, result["cu_recovered_tons"] + result["tail_cu_tons"], places=6)
        status = self.app.mill.finish_batch("mill")
        self.assertEqual("idle", status["state"])
        totals = status["totals"]
        self.assertEqual(12.0, totals["feed_tons"])
        self.assertEqual(0.1056, totals["cu_recovered_tons"])
        self.assertEqual(result["tail_tons"], totals["tail_tons"])
        batches = self.app.mill.batches()
        self.assertEqual(1, len(batches))
        self.assertEqual(["H-1", "H-2"], batches[0]["heat_ids"])
        self.assertEqual(["SL-1", "SL-2"], batches[0]["ladle_ids"])
        self.assertEqual(0.1056, batches[0]["cu_recovered_tons"])

    def test_recovery_follows_grind_size(self) -> None:
        tap_slag(self.app, "H-1")
        cool_one_ladle(self.app, "SL-1", "H-1", tons=4.0, cu_grade=0.012)
        cool_one_ladle(self.app, "SL-2", "H-1", tons=4.0, cu_grade=0.012)
        tap_slag(self.app, "H-2")
        cool_one_ladle(self.app, "SL-3", "H-2", tons=4.0, cu_grade=0.012)
        cool_one_ladle(self.app, "SL-4", "H-2", tons=4.0, cu_grade=0.012)
        self.app.mill.schedule("mill", tons=8.0, rate_tph=100.0, grind_um=45.0)
        self.app.mill.dose("mill", collector_gpt=38.0, frother_gpt=12.4)
        fine = self.app.mill.run("mill")["result"]
        self.app.mill.finish_batch("mill")
        self.app.mill.schedule("mill", tons=8.0, rate_tph=100.0, grind_um=150.0)
        self.app.mill.dose("mill", collector_gpt=38.0, frother_gpt=12.4)
        coarse = self.app.mill.run("mill")["result"]
        self.app.mill.finish_batch("mill")
        self.assertGreater(fine["recovery"], coarse["recovery"])
        self.assertEqual(2, self.app.mill.status()["batches_completed"])

    def test_actions_registered_for_console_and_cli(self) -> None:
        names = set(self.app.actions)
        for action in (
            "slagyard.arrive",
            "slagyard.assign",
            "slagyard.pour",
            "slagyard.finish_cooling",
            "slagyard.release",
            "mill.schedule",
            "mill.dose",
            "mill.run",
            "mill.finish_batch",
        ):
            self.assertIn(action, names)
        tap_slag(self.app, "H-1")
        result = self.app.invoke(
            "slagyard.arrive",
            {"actor": "ops", "ladle_id": "SL-1", "heat_id": "H-1", "tons": 3.0, "cu_grade": 0.01},
        )
        self.assertEqual(1, result["queue_length"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
