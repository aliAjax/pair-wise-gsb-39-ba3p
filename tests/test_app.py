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

    def _draft_with_change(self, code, change, actor="planner-01"):
        disruption = self.db.create_disruption(actor, {"code": code, "name": code, "starts_at": "2026-10-01T00:00:00+08:00", "ends_at": "2026-10-02T00:00:00+08:00"}, "planner")
        version = disruption["draft_version_id"]
        self.db.add_change(version, actor, change, "planner")
        return disruption, version

    def _publish(self, version, submitter="planner-01", reviewer="reviewer-01"):
        self.db.transition(version, submitter, "planner", "submit")
        self.db.transition(version, reviewer, "reviewer", "approve")
        return self.db.transition(version, reviewer, "reviewer", "publish")

    def test_submit_blocked_by_published_same_line_stop_overlap(self):
        line1 = self.db.list_lines()[0]["id"]
        # An already published scheme occupies line 1 / S2 during 600-780.
        _, v1 = self._draft_with_change(
            "D-CONFLICT-1",
            {"kind": "stop_closure", "line_id": line1, "stop_id": self.stops["S2"],
             "effective_start_minute": 600, "effective_end_minute": 780},
        )
        self._publish(v1)

        # Drafts are saved without any conflict check...
        _, v2 = self._draft_with_change(
            "D-CONFLICT-2",
            {"kind": "skip_stop", "line_id": line1, "stop_id": self.stops["S2"],
             "effective_start_minute": 720, "effective_end_minute": 900},
            actor="planner-02",
        )
        conflicts = self.db.find_version_conflicts(v2)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["version_no"], 1)
        self.assertEqual(conflicts[0]["stop_id"], self.stops["S2"])
        self.assertEqual(conflicts[0]["overlap_minutes"], 60)
        # ...but submission must be refused and the version must stay a draft.
        with self.assertRaises(DomainError) as ctx:
            self.db.transition(v2, "planner-02", "planner", "submit")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.details["conflicts"][0]["overlap_minutes"], 60)
        self.assertEqual(self.db.get_version(v2)["status"], "draft")

    def test_unrelated_line_different_stop_and_disjoint_windows_flow_normally(self):
        line1, line2 = (row["id"] for row in self.db.list_lines()[:2])
        _, occupied = self._draft_with_change(
            "D-FREE-1",
            {"kind": "stop_closure", "line_id": line1, "stop_id": self.stops["S2"],
             "effective_start_minute": 600, "effective_end_minute": 780},
        )
        self._publish(occupied)

        cases = [
            # Different line, same stop/window.
            {"kind": "stop_closure", "line_id": line2, "stop_id": self.stops["S2"],
             "effective_start_minute": 600, "effective_end_minute": 780},
            # Same line, different stop.
            {"kind": "stop_closure", "line_id": line1, "stop_id": self.stops["S3"],
             "effective_start_minute": 600, "effective_end_minute": 780},
            # Same line/stop, windows only touch (780 == 780): zero overlap.
            {"kind": "skip_stop", "line_id": line1, "stop_id": self.stops["S2"],
             "effective_start_minute": 780, "effective_end_minute": 900},
            # Same line/stop, completely disjoint window.
            {"kind": "accessibility_change", "line_id": line1, "stop_id": self.stops["S2"],
             "accessible": False, "effective_start_minute": 900, "effective_end_minute": 1000},
        ]
        for index, change in enumerate(cases):
            _, candidate = self._draft_with_change(f"D-FREE-{index + 2}", change, actor=f"planner-{index + 2}")
            self.assertEqual(self.db.find_version_conflicts(candidate), [])
            published = self._publish(candidate, submitter=f"planner-{index + 2}", reviewer=f"reviewer-{index + 2}")
            self.assertEqual(published["status"], "published")

    def test_publish_rechecks_and_rejects_version_made_stale_during_review(self):
        line1 = self.db.list_lines()[0]["id"]
        # Two competing schemes both reach review before either is approved:
        # reviewers cannot see each other's drafts/reviews, so both submit OK.
        _, version_a = self._draft_with_change(
            "D-RACE-1",
            {"kind": "stop_closure", "line_id": line1, "stop_id": self.stops["S4"],
             "effective_start_minute": 800, "effective_end_minute": 1000},
        )
        _, version_b = self._draft_with_change(
            "D-RACE-2",
            {"kind": "stop_closure", "line_id": line1, "stop_id": self.stops["S4"],
             "effective_start_minute": 900, "effective_end_minute": 1100},
            actor="planner-02",
        )
        self.db.transition(version_a, "planner-01", "planner", "submit")
        self.db.transition(version_b, "planner-02", "planner", "submit")

        # Scheme B is approved and published first while A is still in review.
        self.db.transition(version_b, "reviewer-02", "reviewer", "approve")
        self.db.transition(version_b, "reviewer-02", "reviewer", "publish")

        # A only gets approved afterwards; publish must re-check and refuse the
        # now stale scheme (100 min overlap with the published B).
        self.db.transition(version_a, "reviewer-01", "reviewer", "approve")
        with self.assertRaises(DomainError) as ctx:
            self.db.transition(version_a, "reviewer-01", "reviewer", "publish")
        self.assertEqual(ctx.exception.status, 409)
        conflicts = ctx.exception.details["conflicts"]
        self.assertEqual(conflicts[0]["version_no"], 1)
        self.assertEqual(conflicts[0]["stop_id"], self.stops["S4"])
        self.assertEqual(conflicts[0]["overlap_minutes"], 100)
        self.assertEqual(self.db.get_version(version_a)["status"], "approved")
        # B remains the one published version.
        self.assertEqual(self.db.get_version(version_b)["status"], "published")

    def test_detour_endpoints_and_all_day_windows_conflict(self):
        line1 = self.db.list_lines()[0]["id"]
        # A whole-day closure (no effective window) of S3 occupies it 0-2880.
        _, whole_day = self._draft_with_change(
            "D-ALLDAY-1",
            {"kind": "stop_closure", "line_id": line1, "stop_id": self.stops["S3"]},
        )
        self._publish(whole_day)

        # A detour touching S3 inside a short window conflicts (full window intersection).
        _, detour = self._draft_with_change(
            "D-ALLDAY-2",
            {"kind": "detour", "line_id": line1, "from_stop_id": self.stops["S3"],
             "to_stop_id": self.stops["S5"], "travel_minutes": 12,
             "effective_start_minute": 300, "effective_end_minute": 400},
            actor="planner-02",
        )
        conflicts = self.db.find_version_conflicts(detour)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["stop_id"], self.stops["S3"])
        self.assertEqual(conflicts[0]["overlap_minutes"], 100)
        with self.assertRaises(DomainError):
            self.db.transition(detour, "planner-02", "planner", "submit")

        # A detour touching only S5 keeps flowing.
        _, free_detour = self._draft_with_change(
            "D-ALLDAY-3",
            {"kind": "detour", "line_id": line1, "from_stop_id": self.stops["S1"],
             "to_stop_id": self.stops["S5"], "travel_minutes": 20,
             "effective_start_minute": 300, "effective_end_minute": 400},
            actor="planner-03",
        )
        self.assertEqual(self.db.find_version_conflicts(free_detour), [])
        self.assertEqual(self._publish(free_detour, "planner-03", "reviewer-03")["status"], "published")


if __name__ == "__main__":
    unittest.main()
