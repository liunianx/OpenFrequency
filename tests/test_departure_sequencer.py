"""阶段四（工作块 D）回归测试：DepartureSequencer 与 atc_action 钩子链。

尾流间隔按计划 §7.D1 的工程默认表逐组对拍（含 Super/Heavy/Medium/Light）。

运行：python -m unittest discover -s tests -v
"""
import threading
import unittest

from core.atc_session import ATCSession
from core.departure_sequencer import DepartureSequencer, _runway_group
from core.taxi_router import TaxiRouter
from core.traffic_manager import AircraftTrackingData, TrafficState, TrafficStateManager

LAYOUT = {
    "taxi_nodes": [
        {"id": "n1", "lat": 40.0000, "lon": 116.0, "usage": "both"},
        {"id": "n2", "lat": 40.0010, "lon": 116.0, "usage": "both"},
        {"id": "n3", "lat": 40.0020, "lon": 116.0, "usage": "both"},
    ],
    "taxi_edges": [
        {"start": "n1", "end": "n2", "kind": "taxiway", "name": "A"},
        {"start": "n2", "end": "n3", "kind": "runway", "name": "runway 18/36"},
    ],
    "startup_locations": [],
}


class FakeGroundService:
    def get_airport_layout(self, icao):
        return LAYOUT

    def update_config(self, config):
        pass


class FakeTrafficManager:
    """最小交通表：lock + aircraft(dict)。"""

    def __init__(self, entries):
        self.lock = threading.RLock()
        self.aircraft = entries


def _ac(callsign, lat, wake="M", state=TrafficState.TAXIING, runway=None, speed=10):
    ac = AircraftTrackingData(callsign=callsign, voice_id=None)
    ac.latitude, ac.longitude = lat, 116.0
    ac.airspeed = speed
    ac.state = state
    ac.wake_category = wake
    ac.assigned_runway = runway
    return ac


def _sequencer(entries, config=None):
    router = TaxiRouter(FakeGroundService())
    router.build_graph_for_airport("ZBAA")
    return DepartureSequencer(config or {}, traffic_manager=FakeTrafficManager(entries),
                              taxi_router=router)


class WakeSeparationTableTests(unittest.TestCase):
    """§7.D1 工程默认表逐组对拍。"""

    def setUp(self):
        self.seq = DepartureSequencer({})

    def test_super_row(self):
        self.assertEqual(self.seq.wake_gap_seconds("J", "J"), 120)
        self.assertEqual(self.seq.wake_gap_seconds("J", "H"), 120)
        self.assertEqual(self.seq.wake_gap_seconds("J", "M"), 180)
        self.assertEqual(self.seq.wake_gap_seconds("J", "L"), 180)

    def test_heavy_row(self):
        self.assertEqual(self.seq.wake_gap_seconds("H", "H"), 90)
        self.assertEqual(self.seq.wake_gap_seconds("H", "M"), 120)
        self.assertEqual(self.seq.wake_gap_seconds("H", "L"), 180)

    def test_medium_and_light_rows(self):
        self.assertEqual(self.seq.wake_gap_seconds("M", "M"), 60)
        self.assertEqual(self.seq.wake_gap_seconds("M", "L"), 120)
        self.assertEqual(self.seq.wake_gap_seconds("L", "L"), 60)

    def test_no_mandatory_separation_uses_cooldown(self):
        # Medium/Light 前机对 Heavy/Super 无强制尾流间隔 → 60s 冷却
        self.assertEqual(self.seq.wake_gap_seconds("M", "H"), 60)
        self.assertEqual(self.seq.wake_gap_seconds("L", "J"), 60)

    def test_unknown_category_uses_conservative_default(self):
        self.assertEqual(self.seq.wake_gap_seconds("UNKNOWN", "M"), 120)
        self.assertEqual(self.seq.wake_gap_seconds("H", "UNKNOWN"), 120)

    def test_config_override(self):
        seq = DepartureSequencer({"traffic": {"wake_sep_seconds": 200}})
        self.assertEqual(seq.wake_gap_seconds("J", "L"), 200)
        self.assertEqual(seq.wake_gap_seconds("L", "L"), 200)


class RunwayGroupTests(unittest.TestCase):
    def test_reciprocals_grouped(self):
        self.assertEqual(_runway_group("18"), _runway_group("36"))
        self.assertEqual(_runway_group("36L"), _runway_group("18R"))
        self.assertNotEqual(_runway_group("18L"), _runway_group("18R"))
        self.assertEqual(_runway_group("RW36R"), _runway_group("18L"))
        self.assertEqual(_runway_group("02"), _runway_group("20"))


class QueueBuildTests(unittest.TestCase):
    def test_queue_ordered_by_distance_and_slot_math(self):
        entries = {
            "A1": _ac("A1", 40.0018, wake="H", runway="36"),   # 距 holding point 22 m
            "A2": _ac("A2", 40.0010, wake="M", runway="36"),   # 就在 holding point 上
            "A3": _ac("A3", 40.0014, wake="J", runway="18"),   # 反向呼号同跑道，44 m
        }
        seq = _sequencer(entries)
        seq.rebuild(own_runway="18", own_position={"lat": 40.0021, "lon": 116.0})
        queue = seq.get_queue("18")
        self.assertEqual([item["callsign"] for item in queue], ["A2", "A1", "A3"])
        self.assertEqual(queue[0]["state"], "TAXIING")

        slot = seq.request_takeoff_slot("A2", "18")
        self.assertEqual(slot["position"], 1)
        self.assertEqual(slot["wait_seconds"], 0)
        self.assertIsNone(slot["leader"])

        slot2 = seq.request_takeoff_slot("A1", "18")
        self.assertEqual(slot2["position"], 2)
        self.assertEqual(slot2["leader"], "A2")
        self.assertEqual(slot2["wait_seconds"], 60)    # M 前机 → H 后机：无强制间隔，60s 冷却

        slot3 = seq.request_takeoff_slot("A3", "18")
        self.assertEqual(slot3["position"], 3)
        self.assertEqual(slot3["leader"], "A1")
        self.assertEqual(slot3["wait_seconds"], 60)    # H 前机 → J 后机：无强制间隔，60s 冷却

    def test_empty_queue_clears_immediately(self):
        seq = _sequencer({})
        seq.rebuild(own_runway="18L", own_position={"lat": 40.002, "lon": 116.0})
        slot = seq.request_takeoff_slot("CES123", "18L")
        self.assertEqual(slot["position"], 1)
        self.assertEqual(slot["wait_seconds"], 0)

    def test_graph_distance_fallback_without_assigned_runway(self):
        entries = {
            "B1": _ac("B1", 40.0018, wake="M", runway=None),
            "AIR": _ac("AIR", 40.05, wake="M", runway=None),   # 高空/不在场，不该进地面队列
        }
        entries["AIR"].on_ground = False
        seq = _sequencer(entries)
        seq.rebuild(own_runway="18", own_position={"lat": 40.0021, "lon": 116.0})
        queue = seq.get_queue("18")
        self.assertEqual([item["callsign"] for item in queue], ["B1"])
        self.assertEqual(queue[0]["runway_match"], "graph_distance")

    def test_unknown_wake_coarsened_and_flagged_estimated(self):
        entries = {"C1": _ac("C1", 40.0018, wake="UNKNOWN", runway="18")}
        entries["C1"].aircraft_type = "A320"
        seq = _sequencer(entries)
        seq.rebuild(own_runway="18")
        queue = seq.get_queue("18")
        self.assertEqual(queue[0]["wake_category"], "M")   # 机型粗分
        self.assertTrue(seq.queue_snapshot("18")["estimated"])


class LogicManagerWiringTests(unittest.TestCase):
    """D2：Tier-0 排队插点与 atc_action emit。"""

    def _build(self, entries):
        from core.logic_manager import LogicManager
        cfg = {"frequencies": {}, "audio": {"stt_language": "zh"}, "debug": {},
               "traffic": {"sequencer_enabled": True}}

        class _IO:
            def __init__(self):
                self.chat = []
                self.events = []

            def emit(self, event, data=None, *a, **k):
                if event == "chat_log":
                    self.chat.append((data or {}).get("text", ""))

            @property
            def last(self):
                return self.chat[-1] if self.chat else ""

        io = _IO()
        session = ATCSession(cfg, None)
        session.attach({})
        lm = LogicManager(cfg, io, atc_session=session)
        lm.log_file = "NUL"
        seq = _sequencer(entries)
        lm.departure_sequencer = seq
        # 记录 atc_action emit
        emitted = []
        from core.context import event_bus
        event_bus.on('atc_action', lambda action, params=None: emitted.append(action))
        return lm, session, io, emitted

    def _prime_tower_dep(self, session):
        session.advance_to("TOWER_DEP")
        session.assign("squawk", "4231", by="放行")
        session.propose_runway("18", by="放行")

    def test_nonempty_queue_blocks_immediate_takeoff(self):
        entries = {"A1": _ac("A1", 40.0018, wake="H", runway="18")}
        lm, session, io, emitted = self._build(entries)
        from core.context import shared_context, context_lock
        with context_lock:
            shared_context['atc_state']['current_frequency_role'] = 'Tower'
        self._prime_tower_dep(session)
        lm.process_llm_request("申请起飞")
        self.assertIn("排在第 2 位", io.last)
        self.assertIn("A1", io.last)
        self.assertIn("departure_queue", emitted)

    def test_empty_queue_emits_cleared_takeoff(self):
        lm, session, io, emitted = self._build({})
        from core.context import shared_context, context_lock
        with context_lock:
            shared_context['atc_state']['current_frequency_role'] = 'Tower'
        self._prime_tower_dep(session)
        # 没有 AI 交通 → 队列空 → Tier-0 不拦截，落到 Tier 1/2/3
        lm.process_llm_request("申请起飞")
        self.assertIn("cleared_takeoff", emitted)

    def test_derive_atc_actions_from_cards(self):
        from core.logic_manager import LogicManager
        actions = LogicManager._derive_atc_actions(
            [{"type": "HDG", "value": "090", "label": "Heading"},
             {"type": "SQ", "value": "4231", "label": "Squawk"}],
            "fly heading 090")
        self.assertIn("fly_heading", actions)
        self.assertIn("squawk", actions)
        takeoff = LogicManager._derive_atc_actions([], "CES123, runway 18L, cleared for takeoff.")
        self.assertIn("cleared_takeoff", takeoff)

    def test_traffic_state_change_rebuilds_queue(self):
        entries = {"A1": _ac("A1", 40.0018, wake="H", runway="18")}
        lm, session, io, emitted = self._build(entries)
        self._prime_tower_dep(session)
        from core.context import shared_context, context_lock
        lm._on_traffic_state_change({'callsign': 'A1', 'new_state': 'TAXIING'})
        with context_lock:
            snap = shared_context['atc_state'].get('departure_queue')
        self.assertIsNotNone(snap)
        self.assertEqual(snap['queue'][0]['callsign'], 'A1')


if __name__ == "__main__":
    unittest.main(verbosity=2)
