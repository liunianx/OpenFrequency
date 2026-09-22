"""阶段一（工作块 A）行为回归测试：DISPATCH/PDC、推出前置、到达侧停机位与进港滑行。

运行：python -m unittest discover -s tests -v
"""
import unittest

from core.atc_session import ATCSession, detect_intent
from core.gate_assigner import assign_gate, compute_occupied_stands
from core.taxi_router import TaxiRouter


class FakeFreqService:
    """最小频率库：ZGGG 有 ATIS/CD/GND/TWR/APP，无独立离场频率。"""

    FREQS = {
        "ZGGG": {"ATIS": 128.6, "Clearance Delivery": 121.95,
                 "Ground": 121.75, "Tower": 118.1, "Approach": 120.4},
    }
    RUNWAYS = {"ZGGG": ["01L", "19R", "01R", "19L", "02L", "20R", "02R", "20L", "03", "21"]}

    def get_airport_position(self, ident):
        return {"lat": 23.3924, "lon": 113.2988}

    def get_airport_frequencies(self, ident):
        return [{"role": role, "frequency_mhz": freq,
                 "label": f"{role} {freq:.3f}", "description": role}
                for role, freq in self.FREQS.get((ident or "").upper(), {}).items()]

    def get_frequency_map(self, ident):
        return {role: f"{freq:.3f}" for role, freq in self.FREQS.get((ident or "").upper(), {}).items()}

    def get_nearby_airports(self, lat, lon, sqlite_path=None):
        if lat is None:
            return []
        return [{"ident": "ZGGG", "name": "Baiyun", "lat": lat, "lon": lon,
                 "distance_nm": 2.0, "frequencies": self.get_airport_frequencies("ZGGG")}]

    def get_airport_runways(self, ident):
        return list(self.RUNWAYS.get((ident or "").upper(), []))

    def get_preferred_runways(self, ident, wind_dir=None, limit=2):
        return list(self.RUNWAYS.get((ident or "").upper(), []))[:limit or 2]


SAMPLE_LAYOUT = {
    "taxi_nodes": [
        {"id": "n1", "lat": 40.0000, "lon": 116.0, "usage": "both"},
        {"id": "n2", "lat": 40.0010, "lon": 116.0, "usage": "both"},
        {"id": "n3", "lat": 40.0020, "lon": 116.0, "usage": "both"},
    ],
    "taxi_edges": [
        {"start": "n1", "end": "n2", "kind": "taxiway", "name": "A"},
        {"start": "n2", "end": "n3", "kind": "runway", "name": "runway 18/36"},
    ],
    # simulator/apt.dat 源：startup_locations 是元数据，stand 本身不是图节点
    "startup_locations": [
        {"gate_id": "G1", "lat": 40.0005, "lon": 116.0, "heading": 270,
         "type": "gate", "operation": "Gates"},
        {"gate_id": "C1", "lat": 40.0015, "lon": 116.0,
         "type": "cargo", "operation": "Cargo"},
    ],
}


class FakeGroundService:
    def get_airport_layout(self, icao):
        return SAMPLE_LAYOUT

    def update_config(self, config):
        pass


class StandGraphTests(unittest.TestCase):
    """G3/A3：stand 入图后进港滑行才有稳定终点。"""

    def setUp(self):
        self.router = TaxiRouter(FakeGroundService())
        self.router.build_graph_for_airport("ZBAA")

    def test_stands_become_graph_nodes(self):
        self.assertIn("stand:G1", self.router.graph.nodes)
        self.assertIn("stand:C1", self.router.graph.nodes)

    def test_stand_attached_to_nearest_taxi_node(self):
        # G1 距 n2 约 56 m（< 120 m 阈值）必须连上
        self.assertIn("n2", self.router.graph.neighbors("stand:G1"))

    def test_runway_adjacent_nodes_marked(self):
        self.assertEqual(self.router.graph.nodes["n3"]["runway_links"], 1)
        self.assertTrue(self.router.graph.nodes["n3"]["hotspot"])
        self.assertEqual(self.router.graph.nodes["n1"]["runway_links"], 0)

    def test_taxi_in_route_reaches_stand(self):
        route = self.router.suggest_taxi_in_route(
            "ZBAA", {"lat": 40.0021, "lon": 116.0}, "C1")
        self.assertIsNotNone(route)
        self.assertEqual(route["end_node"], "stand:C1")
        self.assertEqual(route["stand"], "C1")
        self.assertEqual(route["runway_crossings"], 1)
        self.assertEqual(route["path"][0], "n3")  # 起点是跑道邻接节点

    def test_taxi_in_route_unknown_stand_returns_none(self):
        self.assertIsNone(
            self.router.suggest_taxi_in_route("ZBAA", {"lat": 40.002, "lon": 116.0}, "NOPE"))

    def test_pushback_direction_prefers_stand_heading(self):
        # apt.dat 源 heading=270 → west
        self.assertEqual(
            self.router.suggest_pushback_direction(SAMPLE_LAYOUT["startup_locations"][0], "ZBAA"),
            "west")

    def test_pushback_direction_falls_back_to_taxiway_bearing(self):
        # 无 heading 的 stand 退化为指向最近滑行节点的方位
        direction = self.router.suggest_pushback_direction(
            SAMPLE_LAYOUT["startup_locations"][1], "ZBAA")
        self.assertIn(direction, ("north", "south", "east", "west",
                                  "north-east", "north-west", "south-east", "south-west"))


class GateAssignerTests(unittest.TestCase):

    def test_heavy_prefers_gates(self):
        gate = assign_gate(SAMPLE_LAYOUT["startup_locations"], aircraft_size="heavy")
        self.assertEqual(gate["stand"], "G1")

    def test_occupied_stand_skipped(self):
        occupied = compute_occupied_stands(
            SAMPLE_LAYOUT["startup_locations"],
            [{"lat": 40.0005, "lon": 116.0, "on_ground": True}])
        self.assertEqual(occupied, {"G1"})
        gate = assign_gate(SAMPLE_LAYOUT["startup_locations"], "heavy", occupied=occupied)
        self.assertEqual(gate["stand"], "C1")

    def test_airborne_traffic_is_not_occupying(self):
        occupied = compute_occupied_stands(
            SAMPLE_LAYOUT["startup_locations"],
            [{"lat": 40.0005, "lon": 116.0, "on_ground": False}])
        self.assertEqual(occupied, set())

    def test_no_stands_returns_none(self):
        self.assertIsNone(assign_gate([], "medium"))


class LogicManagerDispatchTests(unittest.TestCase):
    """DISPATCH/PDC 与推出状态在 LogicManager 接线后的端到端行为。"""

    def _build(self, rules="IFR"):
        from core.logic_manager import LogicManager
        cfg = {"frequencies": {"Center": "132.500"},
               "audio": {"stt_language": "zh"}, "debug": {},
               "user_profile": {}, "navdata": {}}
        svc = FakeFreqService()
        session = ATCSession(cfg, svc)
        session.attach({})

        class _IO:
            def __init__(self):
                self.chat = []

            def emit(self, event, data=None, *a, **k):
                if event == "chat_log":
                    self.chat.append((data or {}).get("text", ""))

            @property
            def last(self):
                return self.chat[-1] if self.chat else ""

        io = _IO()
        lm = LogicManager(cfg, io, airport_frequency_service=svc,
                          ground_service=FakeGroundService(), atc_session=session)
        lm.log_file = "NUL"
        lm._sync_flight_rules()
        return lm, session, io

    def test_pdc_request_advances_dispatch_to_atis(self):
        from core.context import shared_context, context_lock
        with context_lock:
            shared_context['flight_rules'] = 'IFR'
        lm, session, io = self._build()
        lm.on_flight_plan_loaded({"origin": "ZGGG", "destination": "ZGSZ",
                                  "cruise_alt": 25000})
        self.assertEqual(session.phase, "DISPATCH")
        lm.process_llm_request("申请预放行")
        self.assertIn("predeparture clearance", io.last)
        self.assertEqual(session.phase, "ATIS")
        self.assertTrue(session.state["fpl_confirmed"])

    def test_pdc_allowed_while_tuned_to_clearance_frequency(self):
        # E13：DISPATCH 显示的 CD 频率仅供 UI；守听 CD 也不该把 PDC 请求弹回去
        lm, session, io = self._build()
        lm.on_flight_plan_loaded({"origin": "ZGGG", "destination": "ZGSZ",
                                  "cruise_alt": 25000})
        lm.switch_frequency_context(121.95, source="pilot")
        # tune 到 CD 会把阶段对齐到 CLEARANCE；此时再请求 PDC 也不应被 wrong_station 拦截
        result = session.check_request("申请预放行", tuned_role="Clearance Delivery")
        self.assertTrue(result["allowed"])
        # 未调频（tuned_role=None/N/A）同样放行
        session2 = ATCSession({}, None)
        session2.attach({})
        session2.load_from_flight_plan({"origin": "ZGGG", "destination": "ZGSZ"})
        self.assertEqual(session2.phase, "DISPATCH")
        self.assertTrue(session2.check_request("申请预放行", tuned_role=None)["allowed"])
        self.assertTrue(session2.check_request("申请预放行", tuned_role="N/A")["allowed"])

    def test_vfr_skips_dispatch_entirely(self):
        lm, session, io = self._build()
        lm.on_flight_plan_loaded({"origin": "ZGGG", "destination": "ZGSZ",
                                  "flight_rules": "VFR"})
        self.assertEqual(session.phase, "ATIS")
        # VFR 下请求预放行不应产生 PDC 回复
        lm.process_llm_request("申请预放行")
        self.assertNotIn("predeparture clearance", io.last)

    def test_taxi_requires_pushback_then_unlocks(self):
        lm, session, io = self._build()
        lm.on_flight_plan_loaded({"origin": "ZGGG", "destination": "ZGSZ"})
        session.advance_to("GROUND_DEP")
        session.propose_runway("20R", by="广州放行")
        lm.switch_frequency_context(121.75, source="pilot")
        # 已守听地面频率时重定向会退化成 LLM 处理（same_freq 设计），
        # 这里验证 Tier-0 判定本身：prereq 缺失时 check_request 不允许。
        check = session.check_request("申请滑行", tuned_role="Ground")
        self.assertFalse(check["allowed"])
        self.assertEqual(check["reason"], "missing_pushback")
        self.assertEqual(check["redirect"]["role"], "Ground")
        lm.process_llm_request("推出完成")
        self.assertTrue(session.state["pushback_done"])
        check2 = session.check_request("申请滑行", tuned_role="Ground")
        self.assertTrue(check2["allowed"])

    def test_gate_request_assigns_gate_and_taxi_in_route(self):
        from core.context import shared_context, context_lock
        lm, session, io = self._build()
        lm.on_flight_plan_loaded({"origin": "ZGGG", "destination": "ZGGG"})
        session.advance_to("GROUND_ARR")
        with context_lock:
            shared_context['aircraft']['latitude'] = 40.0021
            shared_context['aircraft']['longitude'] = 116.0
        gate = lm._assign_gate("Ground", dict(shared_context))
        self.assertEqual(gate["stand"], "G1")
        self.assertEqual(session.get("assigned_gate"), "G1")
        summary = shared_context['navigation']['ground_layout_summary']
        self.assertEqual(summary['assigned_gate'], "G1")
        self.assertIn('suggested_taxi_in_route', summary)

    def test_full_cycle_ifr_dispatch_to_parked(self):
        """§9 阶段一验收：mock 遥测驱动 DISPATCH→ATIS→…→PARKED 全程推进。"""
        session = ATCSession({"frequencies": {}}, FakeFreqService())
        session.attach({})
        session.load_from_flight_plan({"origin": "ZGGG", "destination": "ZGGG"},
                                      callsign="G-NXWB")
        self.assertEqual(session.phase, "DISPATCH")
        session.confirm_flight_plan()
        self.assertEqual(session.observe_telemetry(True, 0, 0, 0), "ATIS")
        session.mark_atis_copied()
        self.assertEqual(session.observe_telemetry(True, 0, 0, 0), "CLEARANCE")
        session.assign("squawk", "4231", by="广州放行")
        session.propose_runway("20R", by="广州放行")
        session.advance_to("GROUND_DEP")          # 放行后联系地面
        session.mark_pushback_done()
        self.assertEqual(session.observe_telemetry(False, 30, 0, 15), "TOWER_DEP")
        self.assertEqual(session.observe_telemetry(False, 1200, 900, 140), "DEPARTURE")
        self.assertIsNone(session.observe_telemetry(False, 15000, 900, 300))  # 未到巡航
        self.assertEqual(session.observe_telemetry(False, 19000, 0, 430), "CENTER")
        session.state["descending"] = True
        self.assertEqual(session.observe_telemetry(False, 15000, -400, 380), "APPROACH")
        self.assertEqual(session.observe_telemetry(False, 2500, -700, 160), "TOWER_ARR")
        session.propose_runway("02L", by="广州进近", arrival=True)
        self.assertEqual(session.observe_telemetry(True, 0, 0, 60), "GROUND_ARR")
        session.assign("assigned_gate", "G1", by="广州地面")
        self.assertEqual(session.observe_telemetry(True, 0, 0, 0), "PARKED")
        self.assertIsNone(session.observe_telemetry(True, 0, 0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
