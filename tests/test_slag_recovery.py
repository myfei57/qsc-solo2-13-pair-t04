"""渣场缓冷与磨选回收链：到货、缓冷、倒渣、磨选、金属平衡与回收铜去向。"""

from __future__ import annotations

import unittest

from flashsmelter.application import Application
from flashsmelter.config import Settings
from flashsmelter.errors import GuardViolation, NotFoundError, StateTransitionError
from flashsmelter.runtime import ManualClock

from .helpers import feed_heat, make_app, make_root, start_furnace

# 测试用短缓冷：最短 900s，每吨再加 10s。
PLANT_OVERRIDES = {"slagyard_min_cool_seconds": 900.0, "slagyard_cool_seconds_per_ton": 10.0}


def make_plant(**overrides):
    for key, value in PLANT_OVERRIDES.items():
        overrides.setdefault(key, value)
    return make_app(**overrides)


def tap_slag(app, heat_id: str, tons: float) -> None:
    """放渣，形成渣包可到货的额度（沉淀池单次可放量有限，分批放）。"""

    remaining = tons
    while remaining > 1e-9:
        step = min(8.0, remaining)
        app.settler.update("tester", bath_level_m=0.68, slag_thickness_m=0.15, matte_level_m=0.45)
        app.clock.advance(app.settings.settler_layering_dwell_seconds + 1)
        app.settler.settle("tester", heat_id=heat_id)
        app.slag.tap("tester", heat_id=heat_id, target_tons=step)
        remaining -= step


def stock_ladle(app, heat_id: str, ladle_id: str, tons: float, grade_pct: float) -> None:
    """到货 → 化验 → 缓冷 → 倒渣，形成一批待选渣。"""

    app.slagyard.arrive("tester", heat_id=heat_id, ladle_id=ladle_id, tons=tons)
    app.slagyard.assay("tester", ladle_id=ladle_id, cu_grade_pct=grade_pct)
    app.slagyard.schedule_cooling("tester", ladle_id=ladle_id)
    required = app.slagyard.status()["ladles"][ladle_id]["required_cool_seconds"]
    app.clock.advance(required + 1)
    app.slagyard.dump("tester", ladle_id=ladle_id)


def plan_mill(app, *, rate: float = 30.0, fineness: float = 80.0) -> None:
    app.slagmill.set_feed("tester", rate_tph=rate, grind_fineness_pct=fineness)
    app.slagmill.dose("tester", reagent="collector", kg_per_t=0.12)
    app.slagmill.dose("tester", reagent="frother", kg_per_t=0.05)


class SlagYardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_plant()
        start_furnace(self.app)
        feed_heat(self.app, "H-1")

    def test_arrival_requires_tapped_heat(self) -> None:
        with self.assertRaises(NotFoundError):
            self.app.slagyard.arrive("ops", heat_id="H-1", ladle_id="L-1", tons=5.0)

    def test_arrival_respects_tapped_tonnage(self) -> None:
        tap_slag(self.app, "H-1", 8.0)
        self.app.slagyard.arrive("ops", heat_id="H-1", ladle_id="L-1", tons=8.0)
        with self.assertRaises(GuardViolation):
            self.app.slagyard.arrive("ops", heat_id="H-1", ladle_id="L-2", tons=1.0)
        with self.assertRaises(GuardViolation):  # 超过渣包容量上限
            self.app.slagyard.arrive("ops", heat_id="H-1", ladle_id="L-3", tons=35.0)
        with self.assertRaises(GuardViolation):  # 吨位必须为正
            self.app.slagyard.arrive("ops", heat_id="H-1", ladle_id="L-4", tons=0.0)

    def test_duplicate_active_ladle_rejected_and_reuse_after_dump(self) -> None:
        tap_slag(self.app, "H-1", 8.0)
        self.app.slagyard.arrive("ops", heat_id="H-1", ladle_id="L-1", tons=4.0)
        with self.assertRaises(GuardViolation):
            self.app.slagyard.arrive("ops", heat_id="H-1", ladle_id="L-1", tons=4.0)
        self.app.slagyard.assay("ops", ladle_id="L-1", cu_grade_pct=0.9)
        self.app.slagyard.schedule_cooling("ops", ladle_id="L-1")
        self.app.clock.advance(1000)
        self.app.slagyard.dump("ops", ladle_id="L-1")
        status = self.app.slagyard.arrive("ops", heat_id="H-1", ladle_id="L-1", tons=4.0)
        self.assertEqual("arrived", status["ladles"]["L-1"]["status"])
        self.assertEqual(1, len(status["dumped_recent"]))
        self.assertEqual("dumped", status["dumped_recent"][0]["status"])

    def test_cooling_bay_assignment_and_full(self) -> None:
        app = make_plant(slagyard_cooling_bays=1)
        start_furnace(app)
        feed_heat(app, "H-1")
        tap_slag(app, "H-1", 16.0)
        app.slagyard.arrive("ops", heat_id="H-1", ladle_id="L-1", tons=8.0)
        app.slagyard.arrive("ops", heat_id="H-1", ladle_id="L-2", tons=8.0)
        app.slagyard.schedule_cooling("ops", ladle_id="L-1")
        status = app.slagyard.status()
        self.assertEqual(0, status["free_bays"])
        self.assertEqual("L-1", status["bays"]["BAY-1"])
        with self.assertRaises(GuardViolation):  # 缓冷位已满
            app.slagyard.schedule_cooling("ops", ladle_id="L-2")
        with self.assertRaises(GuardViolation):  # 已在缓冷，不能重复排
            app.slagyard.schedule_cooling("ops", ladle_id="L-1")
        app.slagyard.assay("ops", ladle_id="L-1", cu_grade_pct=1.0)
        app.clock.advance(1000)
        app.slagyard.dump("ops", ladle_id="L-1")
        app.slagyard.schedule_cooling("ops", ladle_id="L-2")
        self.assertEqual("L-2", app.slagyard.status()["bays"]["BAY-1"])

    def test_dump_requires_cooling_complete(self) -> None:
        tap_slag(self.app, "H-1", 8.0)
        self.app.slagyard.arrive("ops", heat_id="H-1", ladle_id="L-1", tons=8.0)
        self.app.slagyard.assay("ops", ladle_id="L-1", cu_grade_pct=1.1)
        self.app.slagyard.schedule_cooling("ops", ladle_id="L-1")
        with self.assertRaises(GuardViolation) as blocked:
            self.app.slagyard.dump("ops", ladle_id="L-1")
        self.assertIn("remaining_cool_seconds", blocked.exception.details)
        self.app.clock.advance(1000)
        status = self.app.slagyard.dump("ops", ladle_id="L-1")
        self.assertEqual(8.0, status["stock_tons"])

    def test_dump_requires_assay(self) -> None:
        tap_slag(self.app, "H-1", 8.0)
        self.app.slagyard.arrive("ops", heat_id="H-1", ladle_id="L-1", tons=8.0)
        self.app.slagyard.schedule_cooling("ops", ladle_id="L-1")
        self.app.clock.advance(1000)
        with self.assertRaises(GuardViolation):
            self.app.slagyard.dump("ops", ladle_id="L-1")
        self.app.slagyard.assay("ops", ladle_id="L-1", cu_grade_pct=0.8)
        self.assertEqual(8.0, self.app.slagyard.dump("ops", ladle_id="L-1")["stock_tons"])

    def test_dump_moves_to_stock_and_reports_cooling(self) -> None:
        tap_slag(self.app, "H-1", 8.0)
        self.app.slagyard.arrive("ops", heat_id="H-1", ladle_id="L-1", tons=8.0)
        self.app.slagyard.assay("ops", ladle_id="L-1", cu_grade_pct=1.25)
        self.app.slagyard.schedule_cooling("ops", ladle_id="L-1")
        cooling = self.app.slagyard.status()["cooling"]
        self.assertEqual(1, len(cooling))
        self.assertFalse(cooling[0]["ready_to_dump"])
        self.app.clock.advance(1000)
        cooling = self.app.slagyard.status()["cooling"]
        self.assertTrue(cooling[0]["ready_to_dump"])
        status = self.app.slagyard.dump("ops", ladle_id="L-1")
        self.assertEqual("empty", status["state"])
        self.assertEqual(8.0, status["stock_tons"])
        self.assertAlmostEqual(0.1, status["stock_cu_tons"], places=4)
        self.assertEqual("H-1", status["stock"][0]["heat_id"])
        self.assertEqual(1.25, status["stock"][0]["cu_grade_pct"])
        self.assertEqual(self.app.settings.slagyard_cooling_bays, status["free_bays"])
        dumps = self.app.store.read_stream("slagyard/dumps", limit=5)
        self.assertEqual(1, len(dumps))
        self.assertEqual("L-1", dumps[0].payload["ladle_id"])


class SlagMillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_plant()
        start_furnace(self.app)
        feed_heat(self.app, "H-1")
        tap_slag(self.app, "H-1", 16.0)

    def _stock_default(self) -> None:
        stock_ladle(self.app, "H-1", "L-1", 8.0, 1.0)
        stock_ladle(self.app, "H-1", "L-2", 8.0, 1.4)

    def test_run_requires_feed_plan(self) -> None:
        self._stock_default()
        with self.assertRaises(StateTransitionError):
            self.app.slagmill.run("tester", tons=8.0)
        with self.assertRaises(StateTransitionError):  # 未安排给矿不能加药
            self.app.slagmill.dose("tester", reagent="collector", kg_per_t=0.1)

    def test_set_feed_validation(self) -> None:
        with self.assertRaises(GuardViolation):
            self.app.slagmill.set_feed("tester", rate_tph=0.0, grind_fineness_pct=80.0)
        with self.assertRaises(GuardViolation):  # 超磨机能力
            self.app.slagmill.set_feed("tester", rate_tph=70.0, grind_fineness_pct=80.0)
        with self.assertRaises(GuardViolation):  # 细度低于下限
            self.app.slagmill.set_feed("tester", rate_tph=30.0, grind_fineness_pct=50.0)
        with self.assertRaises(GuardViolation):  # 细度高于上限
            self.app.slagmill.set_feed("tester", rate_tph=30.0, grind_fineness_pct=97.0)
        self.assertEqual("idle", self.app.slagmill.status()["state"])

    def test_run_requires_dosing(self) -> None:
        self._stock_default()
        self.app.slagmill.set_feed("tester", rate_tph=30.0, grind_fineness_pct=80.0)
        with self.assertRaises(GuardViolation):
            self.app.slagmill.run("tester", tons=8.0)
        self.app.slagmill.dose("tester", reagent="collector", kg_per_t=0.12)
        self.app.slagmill.dose("tester", reagent="collector", kg_per_t=0.0)  # 停药
        with self.assertRaises(GuardViolation):
            self.app.slagmill.run("tester", tons=8.0)
        with self.assertRaises(GuardViolation):  # 加药量超上限
            self.app.slagmill.dose("tester", reagent="collector", kg_per_t=2.0)

    def test_run_rejects_insufficient_stock(self) -> None:
        self._stock_default()
        plan_mill(self.app)
        with self.assertRaises(GuardViolation):
            self.app.slagmill.run("tester", tons=999.0)
        with self.assertRaises(GuardViolation):
            self.app.slagmill.run("tester", tons=0.0)

    def test_run_records_full_mass_balance(self) -> None:
        self._stock_default()
        plan_mill(self.app)
        status = self.app.slagmill.run("tester", tons=16.0)
        self.assertEqual(1, status["batches_completed"])
        self.assertAlmostEqual(0.192, status["feed_cu_tons"], places=3)
        self.assertAlmostEqual(0.874, status["recovery_rate"], places=3)
        self.assertAlmostEqual(0.1678, status["recovered_cu_tons"], places=3)
        self.assertAlmostEqual(0.671, status["concentrate_tons"], places=3)
        self.assertAlmostEqual(15.329, status["tailings_tons"], places=3)
        batches = self.app.slagmill.batches()
        self.assertEqual(1, len(batches))
        batch = batches[0]
        self.assertAlmostEqual(1.2, batch["feed_grade_pct"], places=3)
        self.assertAlmostEqual(
            batch["feed_cu_tons"], batch["recovered_cu_tons"] + batch["tailings_cu_tons"], places=3
        )
        self.assertAlmostEqual(
            batch["feed_tons"], batch["concentrate_tons"] + batch["tailings_tons"], places=3
        )
        self.assertEqual({"collector": 0.12, "frother": 0.05}, batch["dosing"])
        self.assertEqual(2, len(batch["lots"]))
        yard = self.app.slagyard.status()
        self.assertEqual(0.0, yard["stock_tons"])
        self.assertEqual(16.0, yard["consumed_tons"])

    def test_recovery_tracks_grade_and_fineness(self) -> None:
        stock_ladle(self.app, "H-1", "L-1", 8.0, 1.0)
        stock_ladle(self.app, "H-1", "L-2", 8.0, 1.0)
        self.app.slagmill.set_feed("tester", rate_tph=30.0, grind_fineness_pct=60.0)
        self.app.slagmill.dose("tester", reagent="collector", kg_per_t=0.12)
        self.app.slagmill.run("tester", tons=8.0)  # 1.0% @ 细度 60
        self.app.slagmill.set_feed("tester", rate_tph=30.0, grind_fineness_pct=90.0)
        self.app.slagmill.run("tester", tons=8.0)  # 1.0% @ 细度 90
        tap_slag(self.app, "H-1", 16.0)
        stock_ladle(self.app, "H-1", "L-3", 8.0, 2.0)
        stock_ladle(self.app, "H-1", "L-4", 8.0, 2.0)
        self.app.slagmill.set_feed("tester", rate_tph=30.0, grind_fineness_pct=60.0)
        self.app.slagmill.run("tester", tons=8.0)  # 2.0% @ 细度 60
        batches = self.app.slagmill.batches()
        self.assertAlmostEqual(0.79, batches[0]["recovery"], places=3)
        self.assertAlmostEqual(0.91, batches[1]["recovery"], places=3)
        self.assertAlmostEqual(0.81, batches[2]["recovery"], places=3)
        self.assertGreater(batches[1]["recovery"], batches[0]["recovery"])  # 细度高回收高
        self.assertGreater(batches[2]["recovery"], batches[0]["recovery"])  # 品位高回收高

    def test_blend_tracks_recovered_copper(self) -> None:
        self._stock_default()
        plan_mill(self.app)
        status = self.app.slagmill.run("tester", tons=16.0)
        batch_id = status["concentrates"][0]["batch_id"]
        self.assertAlmostEqual(0.671, status["unblended_concentrate_tons"], places=3)
        blended = self.app.slagmill.blend("tester", batch_id=batch_id)
        self.assertEqual(0.0, blended["unblended_concentrate_tons"])
        self.assertTrue(blended["concentrates"][0]["blended"])
        blends = self.app.slagmill.blends()
        self.assertEqual(1, len(blends))
        self.assertEqual(batch_id, blends[0]["batch_id"])
        self.assertEqual("conc", blends[0]["target"])
        with self.assertRaises(GuardViolation):  # 重复并入
            self.app.slagmill.blend("tester", batch_id=batch_id)
        with self.assertRaises(NotFoundError):
            self.app.slagmill.blend("tester", batch_id="slagconc-nope")

    def test_stop_clears_feed_plan(self) -> None:
        self._stock_default()
        plan_mill(self.app)
        status = self.app.slagmill.stop("tester")
        self.assertEqual("idle", status["state"])
        self.assertIsNone(status["feed_plan"])
        with self.assertRaises(StateTransitionError):
            self.app.slagmill.run("tester", tons=8.0)


class SlagChainWiringTest(unittest.TestCase):
    def test_actions_registered(self) -> None:
        app = make_plant()
        names = set(app.actions)
        for name in (
            "slagyard.arrive",
            "slagyard.assay",
            "slagyard.schedule_cooling",
            "slagyard.dump",
            "slagmill.set_feed",
            "slagmill.dose",
            "slagmill.run",
            "slagmill.blend",
            "slagmill.stop",
        ):
            self.assertIn(name, names)

    def test_full_chain_via_action_registry(self) -> None:
        app = make_plant()
        start_furnace(app)
        feed_heat(app, "H-9")
        tap_slag(app, "H-9", 8.0)
        app.invoke("slagyard.arrive", {"actor": "ops", "heat_id": "H-9", "ladle_id": "L-9", "tons": 8.0})
        app.invoke("slagyard.assay", {"actor": "ops", "ladle_id": "L-9", "cu_grade_pct": 1.1})
        app.invoke("slagyard.schedule_cooling", {"actor": "ops", "ladle_id": "L-9"})
        app.clock.advance(1000)
        app.invoke("slagyard.dump", {"actor": "ops", "ladle_id": "L-9"})
        app.invoke("slagmill.set_feed", {"actor": "ops", "rate_tph": 25.0, "grind_fineness_pct": 80.0})
        app.invoke("slagmill.dose", {"actor": "ops", "reagent": "collector", "kg_per_t": 0.1})
        result = app.invoke("slagmill.run", {"actor": "ops", "tons": 8.0})
        self.assertEqual(1, result["batches_completed"])
        batch_id = result["concentrates"][0]["batch_id"]
        blended = app.invoke("slagmill.blend", {"actor": "ops", "batch_id": batch_id})
        self.assertTrue(blended["concentrates"][-1]["blended"])
        blends = app.slagmill.blends()
        self.assertEqual(1, len(blends))
        self.assertEqual("conc", blends[0]["target"])
        self.assertEqual(batch_id, blends[0]["batch_id"])
        state = app.state()
        self.assertIn("slagyard", state["components"])
        self.assertIn("slagmill", state["components"])
        events = app.audit_events(limit=50, action="run")
        self.assertTrue(any(event["outcome"] == "ok" for event in events))

    def test_state_survives_restart(self) -> None:
        root = make_root()
        clock = ManualClock()
        app = Application(Settings(root=root, **PLANT_OVERRIDES), clock=clock)
        start_furnace(app)
        feed_heat(app, "H-1")
        tap_slag(app, "H-1", 16.0)
        stock_ladle(app, "H-1", "L-1", 8.0, 1.2)
        app.slagyard.arrive("tester", heat_id="H-1", ladle_id="L-2", tons=8.0)
        app.slagyard.schedule_cooling("tester", ladle_id="L-2")
        plan_mill(app)
        app.slagmill.run("tester", tons=8.0)
        recovered = app.slagmill.status()["recovered_cu_tons"]

        rebuilt = Application(Settings(root=root, **PLANT_OVERRIDES), clock=clock)
        yard = rebuilt.slagyard.status()
        self.assertEqual("cooling", yard["state"])
        self.assertEqual("cooling", yard["ladles"]["L-2"]["status"])
        self.assertEqual("L-2", yard["bays"]["BAY-1"])
        self.assertEqual(8.0, yard["consumed_tons"])
        mill = rebuilt.slagmill.status()
        self.assertEqual("feeding", mill["state"])
        self.assertEqual(1, mill["batches_completed"])
        self.assertAlmostEqual(recovered, mill["recovered_cu_tons"], places=4)
        self.assertEqual({"collector": 0.12, "frother": 0.05}, mill["dosing"])
        # 缓冷计时基于时钟，重启后继续累计，倒渣不受影响
        rebuilt.clock.advance(1000)
        rebuilt.slagyard.assay("tester", ladle_id="L-2", cu_grade_pct=0.8)
        rebuilt.slagyard.dump("tester", ladle_id="L-2")
        self.assertEqual(8.0, rebuilt.slagyard.status()["stock_tons"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
