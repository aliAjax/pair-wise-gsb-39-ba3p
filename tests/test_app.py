import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


class TransitFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed_demo(self.db)
        self.stops = {row["code"]: row["id"] for row in self.db.list_stops()}

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_route_version_review_publish_and_snapshot_isolation(self):
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"])["minutes"], 23)
        disruption = self.db.create_disruption("planner-01", {"code": "D-001", "name": "会展站跳站", "starts_at": "2026-09-24T22:00:00+08:00", "ends_at": "2026-09-25T02:00:00+08:00"}, "planner")
        v1 = disruption["draft_version_id"]
        self.db.add_change(v1, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S4"]}, "planner")
        with self.assertRaises(DomainError):
            self.db.transition(v1, "planner-01", "planner", "publish")
        self.db.transition(v1, "planner-01", "planner", "submit")
        self.db.transition(v1, "reviewer-01", "reviewer", "approve")
        published = self.db.transition(v1, "reviewer-01", "reviewer", "publish")
        self.assertEqual(published["status"], "published")
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v1)["minutes"], 31)

        v2 = self.db.create_version_copy(disruption["id"], v1, "planner-02", "planner")["id"]
        self.db.add_change(v2, "planner-02", {"kind": "detour", "from_stop_id": self.stops["S1"], "to_stop_id": self.stops["S5"], "travel_minutes": 18}, "planner")
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v2)["minutes"], 18)
        # Publishing v2 as a draft snapshot does not alter the old published v1.
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v1)["minutes"], 31)
        self.db.transition(v2, "planner-02", "planner", "submit")
        self.db.transition(v2, "reviewer-02", "reviewer", "approve")
        self.db.transition(v2, "reviewer-02", "reviewer", "publish")
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v1)["minutes"], 31)
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v2)["minutes"], 18)

    def test_cross_midnight_times_and_bad_data_isolation(self):
        times = self.db.trip_times(self.db.list_trips()[0]["id"])
        self.assertEqual(times[0]["clock"], "23:50")
        self.assertEqual(times[-1]["service_minute"], 1461)
        self.assertEqual(times[-1]["clock"], "00:21")
        self.assertEqual(times[-1]["day_offset"], 1)

        fresh = Database(Path(self.tmp.name) / "bad.db")
        result = fresh.import_base("planner-01", {
            "lines": [{"code": "B1", "name": "错误线路"}],
            "stops": [{"code": "B-S1", "name": "站点一", "latitude": 31, "longitude": 121}],
            "line_stops": [
                {"line_code": "B1", "stop_code": "B-S1", "sequence": 0, "travel_minutes_from_previous": 0},
                {"line_code": "B1", "stop_code": "NO-SUCH", "sequence": 1, "travel_minutes_from_previous": 5},
            ],
            "trips": [],
        }, "planner")
        self.assertFalse(result["accepted"])
        self.assertTrue(fresh.list_import_errors())
        self.assertEqual(fresh.list_lines(), [])

    def test_accessibility_and_conflict_validation(self):
        disruption = self.db.create_disruption("planner-01", {"code": "D-002", "name": "站点无障碍设施故障", "starts_at": "2026-09-24T00:00:00+08:00", "ends_at": "2026-09-25T00:00:00+08:00"}, "planner")
        version = disruption["draft_version_id"]
        self.db.add_change(version, "planner-01", {"kind": "accessibility_change", "stop_id": self.stops["S4"], "accessible": False}, "planner")
        normal = self.db.route(self.stops["S1"], self.stops["S5"], version, require_accessible=False)
        accessible = self.db.route(self.stops["S1"], self.stops["S5"], version, require_accessible=True)
        self.assertEqual(normal["minutes"], 23)
        self.assertEqual(accessible["minutes"], 31)
        with self.assertRaises(DomainError):
            self.db.add_change(version, "viewer", {"kind": "stop_closure", "stop_id": self.stops["S2"]}, "viewer")


class ConstructionConflictTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed_demo(self.db)
        self.stops = {row["code"]: row["id"] for row in self.db.list_stops()}
        self.lines = {row["code"]: row["id"] for row in self.db.list_lines()}
        self.l1, self.l2 = self.lines["L1"], self.lines["L2"]
        self.window = {"effective_start_minute": 600, "effective_end_minute": 660}

    def tearDown(self):
        self.tmp.cleanup()

    def _disruption(self, code, actor):
        return self.db.create_disruption(actor, {
            "code": code, "name": code + " 施工",
            "starts_at": "2026-09-24T00:00:00+08:00",
            "ends_at": "2026-09-25T00:00:00+08:00",
        }, "planner")

    def _publish_draft(self, version, planner, reviewer):
        self.db.transition(version, planner, "planner", "submit")
        self.db.transition(version, reviewer, "reviewer", "approve")
        self.db.transition(version, reviewer, "reviewer", "publish")

    def test_draft_save_unaffected_but_submit_blocked_by_approved_overlap(self):
        approved = self._disruption("D-A", "planner-01")["draft_version_id"]
        self.db.add_change(approved, "planner-01", {"kind": "stop_closure", "line_id": self.l1, "stop_id": self.stops["S4"], **self.window}, "planner")
        self._publish_draft(approved, "planner-01", "reviewer-01")

        draft = self._disruption("D-B", "planner-02")["draft_version_id"]
        # Saving a draft with an overlapping change is always allowed.
        self.db.add_change(draft, "planner-02", {"kind": "stop_closure", "line_id": self.l1, "stop_id": self.stops["S4"],
                                                 "effective_start_minute": 650, "effective_end_minute": 700}, "planner")
        report = self.db.check_conflicts(draft, "planner-02", "planner")
        self.assertTrue(report["has_conflicts"])
        conflict = report["conflicts"][0]
        self.assertEqual(conflict["other_version_no"], 1)
        self.assertEqual(conflict["line_code"], "L1")
        self.assertEqual(conflict["stop_code"], "S4")
        self.assertEqual(conflict["overlap_minutes"], 10)

        with self.assertRaises(DomainError) as ctx:
            self.db.transition(draft, "planner-02", "planner", "submit")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(len(ctx.exception.details["conflicts"]), 1)
        # A blocked submit leaves the version in draft, so it never enters review.
        self.assertEqual(self.db.get_version(draft)["status"], "draft")

    def test_other_line_stop_disjoint_time_and_detour_endpoints_flow_normally(self):
        approved = self._disruption("D-A", "planner-01")["draft_version_id"]
        self.db.add_change(approved, "planner-01", {"kind": "stop_closure", "line_id": self.l1, "stop_id": self.stops["S4"], **self.window}, "planner")
        self._publish_draft(approved, "planner-01", "reviewer-01")

        different_stop = self._disruption("D-STOP", "planner-02")["draft_version_id"]
        self.db.add_change(different_stop, "planner-02", {"kind": "stop_closure", "line_id": self.l1, "stop_id": self.stops["S2"], **self.window}, "planner")
        self._publish_draft(different_stop, "planner-02", "reviewer-02")

        different_line = self._disruption("D-LINE", "planner-03")["draft_version_id"]
        self.db.add_change(different_line, "planner-03", {"kind": "stop_closure", "line_id": self.l2, "stop_id": self.stops["S4"], **self.window}, "planner")
        self._publish_draft(different_line, "planner-03", "reviewer-03")

        touching = self._disruption("D-TOUCH", "planner-04")["draft_version_id"]
        self.db.add_change(touching, "planner-04", {"kind": "stop_closure", "line_id": self.l1, "stop_id": self.stops["S4"],
                                                    "effective_start_minute": 660, "effective_end_minute": 720}, "planner")
        self._publish_draft(touching, "planner-04", "reviewer-01")

        detour = self._disruption("D-DETOUR", "planner-05")["draft_version_id"]
        # Only S3 and S5 endpoints are occupied, not the closed S4 in between.
        self.db.add_change(detour, "planner-05", {"kind": "detour", "line_id": self.l1, "from_stop_id": self.stops["S3"],
                                                  "to_stop_id": self.stops["S5"], "travel_minutes": 12, **self.window}, "planner")
        self._publish_draft(detour, "planner-05", "reviewer-02")

    def test_publish_recheck_rejects_version_stale_since_approval(self):
        # Both versions reach review before either wins the stop/time slot.
        first = self._disruption("D-A", "planner-01")["draft_version_id"]
        second = self._disruption("D-B", "planner-02")["draft_version_id"]
        for version, planner in ((first, "planner-01"), (second, "planner-02")):
            self.db.add_change(version, planner, {"kind": "stop_closure", "line_id": self.l1, "stop_id": self.stops["S4"], **self.window}, "planner")
            self.db.transition(version, planner, "planner", "submit")

        self.db.transition(first, "reviewer-01", "reviewer", "approve")
        self.db.transition(first, "reviewer-01", "reviewer", "publish")
        # The second version was approved while the first was already published.
        self.db.transition(second, "reviewer-02", "reviewer", "approve")
        with self.assertRaises(DomainError) as ctx:
            self.db.transition(second, "reviewer-02", "reviewer", "publish")
        self.assertEqual(ctx.exception.status, 409)
        self.assertTrue(ctx.exception.details["conflicts"])
        # The stale version stays approved and the first publication is intact.
        self.assertEqual(self.db.get_version(second)["status"], "approved")
        self.assertEqual(self.db.get_version(first)["status"], "published")

    def test_all_day_windows_overlap_full_range(self):
        approved = self._disruption("D-A", "planner-01")["draft_version_id"]
        self.db.add_change(approved, "planner-01", {"kind": "stop_closure", "line_id": self.l1, "stop_id": self.stops["S4"]}, "planner")
        self._publish_draft(approved, "planner-01", "reviewer-01")

        draft = self._disruption("D-B", "planner-02")["draft_version_id"]
        self.db.add_change(draft, "planner-02", {"kind": "stop_closure", "line_id": self.l1, "stop_id": self.stops["S4"], **self.window}, "planner")
        conflict = self.db.check_conflicts(draft)["conflicts"][0]
        self.assertEqual(conflict["overlap_minutes"], 60)

        both_day = self._disruption("D-C", "planner-03")["draft_version_id"]
        self.db.add_change(both_day, "planner-03", {"kind": "stop_closure", "line_id": self.l1, "stop_id": self.stops["S4"]}, "planner")
        self.assertEqual(self.db.check_conflicts(both_day)["conflicts"][0]["overlap_minutes"], 2880)

    def test_revision_chain_of_same_disruption_is_not_conflict(self):
        disruption = self._disruption("D-REV", "planner-01")
        v1 = disruption["draft_version_id"]
        self.db.add_change(v1, "planner-01", {"kind": "stop_closure", "line_id": self.l1, "stop_id": self.stops["S4"]}, "planner")
        self._publish_draft(v1, "planner-01", "reviewer-01")

        # v2 copies the all-day S4 closure from published v1, then narrows it;
        # versions of the same disruption revise rather than collide.
        v2 = self.db.create_version_copy(disruption["id"], v1, "planner-02", "planner")["id"]
        self.db.add_change(v2, "planner-02", {"kind": "stop_closure", "line_id": self.l1, "stop_id": self.stops["S4"], **self.window}, "planner")
        self.assertFalse(self.db.check_conflicts(v2)["has_conflicts"])
        self._publish_draft(v2, "planner-02", "reviewer-02")
        self.assertEqual(self.db.get_version(v2)["status"], "published")


if __name__ == "__main__":
    unittest.main()
