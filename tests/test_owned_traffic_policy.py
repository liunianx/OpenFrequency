"""OwnedTrafficPolicy 回归测试（P3 自动策略，纯决策逻辑）。

运行：python -m unittest discover -s tests -v
"""
import time
import unittest

from core.owned_traffic_policy import (
    OwnedTrafficPolicy,
    generate_callsign,
)


def _rec(owned_id="owned-1", callsign="CES123", spawned_at=1000.0,
         flight_plan=None):
    return {
        "owned_id": owned_id,
        "callsign": callsign,
        "state": "active",
        "object_id": 4242,
        "spawned_at": spawned_at,
        "flight_plan": flight_plan,
    }


class CallsignTests(unittest.TestCase):
    def test_avoids_taken(self):
        callsign = generate_callsign({"CES123"}, index=0)
        self.assertNotEqual(callsign, "CES123")

    def test_case_insensitive(self):
        callsign = generate_callsign({"ces123"}, index=0)
        self.assertNotEqual(callsign.upper(), "CES123")

    def test_deterministic_with_index(self):
        self.assertEqual(generate_callsign(set(), index=42),
                         generate_callsign(set(), index=42))


class SpawnDecisionTests(unittest.TestCase):
    def _policy(self, **owned):
        cfg = {"traffic": {"owned": {"enabled": True, **owned}}}
        return OwnedTrafficPolicy(cfg)

    def test_spawn_when_below_max_and_airport_known(self):
        policy = self._policy(max_aircraft=2)
        actions = policy.on_takeoff_request([], "EGLL", player_position=3)
        spawns = [a for a in actions if a[0] == "spawn"]
        self.assertEqual(len(spawns), 1)
        spec = spawns[0][1]
        self.assertEqual(spec["airport"], "EGLL")
        self.assertTrue(spec["callsign"])

    def test_no_spawn_when_airport_unknown(self):
        policy = self._policy(max_aircraft=2)
        actions = policy.on_takeoff_request([], None, player_position=3)
        self.assertEqual([a for a in actions if a[0] == "spawn"], [])

    def test_no_spawn_at_max(self):
        policy = self._policy(max_aircraft=1)
        actions = policy.on_takeoff_request([_rec()], "EGLL")
        self.assertEqual([a for a in actions if a[0] == "spawn"], [])

    def test_no_spawn_when_disabled(self):
        policy = self._policy(max_aircraft=0)
        actions = policy.on_takeoff_request([], "EGLL")
        self.assertEqual([a for a in actions if a[0] == "spawn"], [])


class ReleaseDecisionTests(unittest.TestCase):
    def _policy(self, **owned):
        cfg = {"traffic": {"owned": {"enabled": True,
                                     "flight_plan": "D:\\\\2.pln", **owned}}}
        return OwnedTrafficPolicy(cfg)

    def test_release_oldest_pending(self):
        policy = self._policy(max_aircraft=3, release_interval_s=0)
        records = [_rec("owned-1", spawned_at=100.0),
                   _rec("owned-2", callsign="CCA456", spawned_at=50.0)]
        actions = policy.on_takeoff_request(records, "EGLL")
        releases = [a for a in actions if a[0] == "release"]
        self.assertEqual(releases, [("release", "owned-2")])   # spawned_at 最小

    def test_release_respects_cooldown(self):
        policy = self._policy(max_aircraft=2, release_interval_s=180)
        policy.note_released()               # 手动登记一次放行
        actions = policy.on_takeoff_request([_rec()], "EGLL")
        self.assertEqual([a for a in actions if a[0] == "release"], [])

    def test_no_release_without_flight_plan_config(self):
        policy = OwnedTrafficPolicy({"traffic": {"owned": {"max_aircraft": 2}}})
        actions = policy.on_takeoff_request([_rec()], "EGLL")
        self.assertEqual([a for a in actions if a[0] == "release"], [])

    def test_already_released_not_re_released(self):
        policy = self._policy(max_aircraft=2, release_interval_s=0)
        records = [_rec(flight_plan="D:\\\\2")]      # 已放行过
        actions = policy.on_takeoff_request(records, "EGLL")
        self.assertEqual([a for a in actions if a[0] == "release"], [])

    def test_spawn_and_release_together(self):
        policy = self._policy(max_aircraft=2, release_interval_s=0)
        actions = policy.on_takeoff_request([_rec()], "EGLL",
                                            player_position=2)
        kinds = [a[0] for a in actions]
        self.assertIn("spawn", kinds)
        self.assertIn("release", kinds)

    def test_failed_records_ignored(self):
        policy = self._policy(max_aircraft=2, release_interval_s=0)
        rec = _rec()
        rec["state"] = "failed"
        actions = policy.on_takeoff_request([rec], "EGLL")
        # failed 不占额度 → 允许 spawn；但绝不能 release 它
        self.assertEqual([a for a in actions if a[0] == "release"], [])
        self.assertEqual(len([a for a in actions if a[0] == "spawn"]), 1)

    def test_records_without_object_id_ignored(self):
        policy = self._policy(max_aircraft=2, release_interval_s=0)
        rec = _rec()
        rec["object_id"] = None       # 还没拿到 objectID 的 pending
        actions = policy.on_takeoff_request([rec], "EGLL")
        self.assertEqual([a for a in actions if a[0] == "release"], [])

    def test_reset_clears_cooldown(self):
        policy = self._policy(max_aircraft=2, release_interval_s=180)
        policy.note_released()
        actions = policy.on_takeoff_request([_rec()], "EGLL")
        self.assertEqual([a for a in actions if a[0] == "release"], [])
        policy.reset()
        actions = policy.on_takeoff_request([_rec()], "EGLL")
        self.assertEqual([a for a in actions if a[0] == "release"],
                         [("release", "owned-1")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
