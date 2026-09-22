"""ATC 联络顺序 / 跨管制员共享状态 的回归测试。

覆盖用户反馈的三个问题：
1. 起飞联络必须从签派（放行）开始，塔台/地面不能越权发放行；
2. 每次得到许可后必须给出下一个联系人的频率；
3. 跑道/应答机在所有管制员之间保持一致，不会被各自另编一个值。

运行：.venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
import unittest

from core.atc_session import (
    ATCSession,
    detect_intent,
    enforce_message,
    extract_runway,
    normalize_runway,
    squawk_for_callsign,
)
from core.logic_manager import LogicManager


class FakeFreqService:
    """最小频率库：模拟 ZGGG 只有 APP/ATIS/CLD/GND/TWR，没有独立离场频率。"""

    FREQS = {
        "ZGGG": {"ATIS": 128.6, "Clearance Delivery": 121.95,
                 "Ground": 121.75, "Tower": 118.1, "Approach": 120.4},
        "VHHH": {"ATIS": 127.0, "Clearance Delivery": 122.0,
                 "Ground": 121.8, "Tower": 118.2, "Approach": 119.1},
    }
    RUNWAYS = {
        "ZGGG": ["01L", "19R", "01R", "19L", "02L", "20R", "02R", "20L", "03", "21"],
        "VHHH": ["07L", "25R", "07R", "25L"],
    }

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


def make_session(phase=None):
    session = ATCSession(config={"frequencies": {"Center": "132.500"}},
                         airport_frequency_service=FakeFreqService())
    session.attach({})
    session.load_from_flight_plan(
        {"origin": "ZGGG", "destination": "VHHH", "cruise_alt": 20700},
        callsign="G-NXWB")
    if phase:
        session.advance_to(phase)
    return session


class RunwayNormalizationTests(unittest.TestCase):
    def test_equivalent_spellings_collapse(self):
        self.assertEqual(normalize_runway("02R"), normalize_runway("2R"))
        self.assertEqual(normalize_runway("20R"), "20R")
        self.assertEqual(normalize_runway("\u8dd1\u9053\u4e8c\u5341\u53f3"), "20R")
        self.assertEqual(normalize_runway("\u4e8c\u5341\u53f3\u8dd1\u9053"), "20R")

    def test_reciprocal_runways_stay_distinct(self):
        # 02R 与 20R 在 ZGGG 是两条不同的跑道，绝不能被当成同一条
        self.assertNotEqual(normalize_runway("02R"), normalize_runway("20R"))

    def test_extract_from_sentence(self):
        self.assertEqual(extract_runway("\u7533\u8bf7\u4f7f\u7528\u8dd1\u905320R"), "20R")
        self.assertEqual(extract_runway("36\u5de6\u8dd1\u9053\uff0c\u53ef\u4ee5\u8d77\u98de"), "36L")
        self.assertIsNone(extract_runway("\u5730\u9762\u98ce310\u5ea68\u8282"))


class SequenceTests(unittest.TestCase):
    """问题 1：起飞联络必须从签派开始。"""

    def test_ifr_starts_at_dispatch_and_pdc_advances(self):
        # IFR 初态为 DISPATCH；PDC 复诵确认（fpl_confirmed）后经遥测推进到 ATIS
        session = make_session()
        self.assertEqual(session.phase, "DISPATCH")
        nxt = session.next_contact()
        self.assertEqual(nxt["role"], "ATIS")
        session.confirm_flight_plan()
        self.assertEqual(
            session.observe_telemetry(on_ground=True, altitude=0, vs=0, groundspeed=0),
            "ATIS")
        session.mark_atis_copied()
        self.assertEqual(
            session.observe_telemetry(on_ground=True, altitude=0, vs=0, groundspeed=0),
            "CLEARANCE")
        contact = session.contact_for("CLEARANCE")
        self.assertEqual(contact["role"], "Clearance Delivery")
        self.assertEqual(contact["frequency"], "121.950")

    def test_vfr_never_starts_at_dispatch(self):
        # E12：VFR/通航不申请 PDC，初态直接 ATIS，且切换 VFR 也不会卡在 DISPATCH
        session = ATCSession(config={}, airport_frequency_service=FakeFreqService())
        session.attach({})
        session.load_from_flight_plan({"origin": "ZGGG", "destination": "VHHH",
                                       "flight_rules": "VFR"}, callsign="G-NXWB")
        self.assertEqual(session.phase, "ATIS")
        self.assertEqual(
            session.observe_telemetry(on_ground=True, altitude=0, vs=0, groundspeed=0),
            None)  # 没抄 ATIS 之前停在 ATIS，不会回 DISPATCH
        # 先按 IFR 建 session 再切 VFR：DISPATCH 必须直接放行到 ATIS
        session2 = make_session()
        session2.set_flight_rules("VFR")
        self.assertEqual(session2.phase, "ATIS")

    def test_dispatch_not_reachable_by_tuning(self):
        # E13：tune 到 CD 频率只能对齐 CLEARANCE，不会命中 DISPATCH
        session = make_session()
        session.observe_tuning("Clearance Delivery")
        self.assertEqual(session.phase, "CLEARANCE")

    def test_first_contact_is_clearance_delivery(self):
        session = make_session()
        session.confirm_flight_plan()
        session.observe_telemetry(on_ground=True, altitude=0, vs=0, groundspeed=0)
        session.mark_atis_copied()
        session.observe_telemetry(on_ground=True, altitude=0, vs=0, groundspeed=0)
        contact = session.contact_for("CLEARANCE")
        self.assertEqual(contact["role"], "Clearance Delivery")
        self.assertEqual(contact["frequency"], "121.950")

    def test_tower_cannot_issue_clearance(self):
        session = make_session()
        session.observe_tuning("Tower")            # 没放行就调塔台
        result = session.check_request("\u7533\u8bf7\u653e\u884c", tuned_role="Tower")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["redirect"]["role"], "Clearance Delivery")
        self.assertEqual(result["redirect"]["frequency"], "121.950")

    def test_ground_cannot_clear_takeoff(self):
        session = make_session("GROUND_DEP")
        session.assign("squawk", "4231", by="\u767d\u4e91\u653e\u884c")
        session.propose_runway("20R", by="\u767d\u4e91\u653e\u884c")
        result = session.check_request("\u7533\u8bf7\u8d77\u98de", tuned_role="Ground")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["redirect"]["role"], "Tower")

    def test_clearance_then_ground_then_tower(self):
        session = make_session()
        session.observe_tuning("Clearance Delivery")
        self.assertTrue(session.check_request("\u7533\u8bf7\u653e\u884c", tuned_role="Clearance Delivery")["allowed"])
        session.assign("squawk", "4231", by="\u767d\u4e91\u653e\u884c")
        session.propose_runway("20R", by="\u767d\u4e91\u653e\u884c")

        self.assertTrue(session.check_request("\u7533\u8bf7\u63a8\u51fa\u5f00\u8f66", tuned_role="Ground")["allowed"])
        self.assertFalse(session.check_request("\u7533\u8bf7\u8d77\u98de", tuned_role="Ground")["allowed"])

        session.observe_tuning("Tower")
        self.assertTrue(session.check_request("\u7533\u8bf7\u8fdb\u5165\u8dd1\u9053\u8d77\u98de", tuned_role="Tower")["allowed"])

    def test_takeoff_blocked_until_clearance_exists(self):
        session = make_session("TOWER_DEP")
        # 阶段已经在塔台，但从没拿到过放行 -> 仍然打放行台
        result = session.check_request("\u7533\u8bf7\u8d77\u98de", tuned_role="Tower")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["redirect"]["role"], "Clearance Delivery")

    def test_no_deadlock_when_phase_lags_behind_tuned_role(self):
        # 频率库没认出放行台导致阶段仍停留在 ATIS 时，也不能把飞行员困死在重定向里
        session = make_session()
        result = session.check_request("\u7533\u8bf7\u653e\u884c", tuned_role="Clearance Delivery")
        self.assertTrue(result["allowed"])
        self.assertEqual(session.phase, "CLEARANCE")

    def test_tuning_back_to_clearance_rolls_phase_back(self):
        session = make_session()
        session.observe_tuning("Tower")
        self.assertEqual(session.phase, "TOWER_DEP")
        session.observe_tuning("Clearance Delivery")
        self.assertEqual(session.phase, "CLEARANCE")


class PushbackAndGateTests(unittest.TestCase):
    """A2/A3：推出前置条件化、停机位与进港滑行意图。"""

    def test_taxi_blocked_without_pushback(self):
        session = make_session("GROUND_DEP")
        result = session.check_request("申请滑行到跑道外等待", tuned_role="Ground")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "missing_pushback")
        # 重定向到地面台（不是放行台），避免死循环劝退
        self.assertEqual(result["redirect"]["role"], "Ground")

    def test_taxi_allowed_after_pushback_done(self):
        session = make_session("GROUND_DEP")
        session.propose_runway("20R", by="白云放行")
        session.mark_pushback_done()
        self.assertTrue(session.check_request("申请滑行", tuned_role="Ground")["allowed"])

    def test_taxi_allowed_when_pushback_ok(self):
        # 站立机位可直接滑出（远距起动位/跑道边）：pushback_ok 放开前置
        session = make_session("GROUND_DEP")
        session.propose_runway("20R", by="白云放行")
        session.set_pushback_ok(True)
        self.assertTrue(session.check_request("申请滑行", tuned_role="Ground")["allowed"])

    def test_pushback_complete_intent_detected(self):
        self.assertEqual(detect_intent("推出完成，申请滑行"), "pushback_complete")
        self.assertEqual(detect_intent("pushback complete, ready for taxi"), "pushback_complete")
        self.assertEqual(detect_intent("engines started"), "engines_started")

    def test_gate_intents_detected(self):
        self.assertEqual(detect_intent("申请停机位"), "gate_request")
        self.assertEqual(detect_intent("滑行到停机位"), "taxi_to_gate")
        # taxi_to_gate 必须排在 taxi 之前，否则"滑行到停机位"被 taxi 吞掉
        self.assertEqual(detect_intent("请求滑行到停机位19"), "taxi_to_gate")

    def test_gate_request_redirected_to_ground_arrival(self):
        session = make_session("TOWER_ARR")
        session.propose_runway("07L", by="香港进近", arrival=True)
        result = session.check_request("申请停机位", tuned_role="Tower")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["redirect"]["role"], "Ground")

    def test_pdc_intent_detected(self):
        self.assertEqual(detect_intent("申请预放行"), "pdc")
        self.assertEqual(detect_intent("request PDC"), "pdc")
        # PDC 必须排在 ifr_clearance 之前，不能被"申请放行"抢先
        self.assertEqual(detect_intent("请求预放行许可"), "pdc")

    def test_star_is_sticky_but_approach_is_not(self):
        # E1：star 一经放行不再变；approach_clearance 保持可覆盖
        session = make_session("CLEARANCE")
        self.assertTrue(session.assign("star", "VYKOS1A", by="广州放行"))
        self.assertFalse(session.assign("star", "ANDOS1A", by="广州塔台"))
        self.assertEqual(session.get("star"), "VYKOS1A")
        self.assertTrue(session.assign("approach_clearance", "ILS 07L", by="香港进近"))
        self.assertTrue(session.assign("approach_clearance", "RNAV 07L", by="香港进近"))
        self.assertEqual(session.get("approach_clearance"), "RNAV 07L")


class NextContactTests(unittest.TestCase):
    """问题 2：每次得到许可后必须给出下一个联系人的频率。"""

    def test_departure_falls_back_to_approach_frequency(self):
        # ZGGG 数据里没有离场频率，必须回退到进近 120.400，而不是 UNKNOWN
        session = make_session("TOWER_DEP")
        nxt = session.next_contact()
        self.assertEqual(nxt["role"], "Departure")
        self.assertEqual(nxt["frequency"], "120.400")

    def test_handoff_without_frequency_gets_one_appended(self):
        session = make_session("TOWER_DEP")
        out, notes = enforce_message("G-NXWB\uff0c\u8054\u7cfb\u79bb\u573a\uff0c\u518d\u89c1\u3002", session)
        self.assertIn("120.400", out)
        self.assertIn("handoff_freq", notes)

    def test_wrong_handoff_frequency_is_corrected(self):
        session = make_session("TOWER_DEP")
        out, _ = enforce_message("G-NXWB\uff0c\u8054\u7cfb\u79bb\u573a 118.100\uff0c\u518d\u89c1\u3002", session)
        self.assertIn("120.400", out)
        self.assertNotIn("118.100", out)

    def test_english_handoff_gets_on_prefix(self):
        session = make_session("TOWER_DEP")
        out, _ = enforce_message("G-NXWB, contact Departure, good day.", session)
        self.assertIn("contact Departure on 120.400", out)

    def test_bare_good_day_gets_full_handoff(self):
        session = make_session("TOWER_DEP")
        out, _ = enforce_message("G-NXWB\uff0c\u53ef\u4ee5\u8d77\u98de\uff0c\u518d\u89c1\u3002", session)
        self.assertIn("\u8054\u7cfb\u79bb\u573a 120.400", out)

    def test_non_handoff_message_untouched(self):
        session = make_session("TOWER_DEP")
        text = "G-NXWB, standby."
        out, notes = enforce_message(text, session)
        self.assertEqual(out, text)
        self.assertEqual(notes, [])

    def test_frequency_query_answered_from_database(self):
        session = make_session("TOWER_DEP")
        answer = session.answer_frequency_query("\u79bb\u573a\u9891\u7387\u662f\u591a\u5c11", "G-NXWB")
        self.assertEqual(answer, "G-NXWB\uff0c\u79bb\u573a\u9891\u7387120.400\u3002")


class SharedMemoryTests(unittest.TestCase):
    """问题 3：跑道/应答机在所有管制员之间保持一致。"""

    def test_squawk_is_stable_per_callsign(self):
        self.assertEqual(squawk_for_callsign("G-NXWB"), squawk_for_callsign("G-NXWB"))
        self.assertNotIn(squawk_for_callsign("G-NXWB"), {"7500", "7600", "7700"})

    def test_second_controller_cannot_change_squawk(self):
        session = make_session("GROUND_DEP")
        self.assertTrue(session.assign("squawk", "4231", by="\u767d\u4e91\u653e\u884c"))
        self.assertFalse(session.assign("squawk", "6712", by="\u767d\u4e91\u5854\u53f0"))
        self.assertEqual(session.get("squawk"), "4231")

    def test_hallucinated_squawk_in_text_is_rewritten(self):
        session = make_session("TOWER_DEP")
        session.assign("squawk", "4231", by="\u767d\u4e91\u653e\u884c")
        out, notes = enforce_message(
            "G-NXWB\uff0c\u53ef\u4ee5\u8d77\u98de\uff0c\u5e94\u7b54\u673a6712\u3002", session)
        self.assertIn("\u5e94\u7b54\u673a4231", out)
        self.assertIn("squawk->4231", notes)

    def test_runway_is_not_silently_changed(self):
        session = make_session("TOWER_DEP")
        session.propose_runway("20R", by="\u767d\u4e91\u653e\u884c")
        ok, display, reason = session.propose_runway("02R", by="\u767d\u4e91\u5854\u53f0")
        self.assertFalse(ok)
        self.assertEqual(reason, "already_assigned")
        self.assertEqual(session.active_runway(), "20R")

    def test_explicit_runway_change_is_allowed(self):
        session = make_session("TOWER_DEP")
        session.propose_runway("20R", by="\u767d\u4e91\u653e\u884c")
        ok, display, _ = session.change_runway("02L", by="\u767d\u4e91\u5854\u53f0")
        self.assertTrue(ok)
        self.assertEqual(normalize_runway(display), "2L")

    def test_unknown_runway_rejected(self):
        session = make_session("CLEARANCE")
        ok, _, reason = session.propose_runway("27", by="\u767d\u4e91\u653e\u884c")
        self.assertFalse(ok)
        self.assertEqual(reason, "not_at_airport")

    def test_runway_rewrite_covers_chinese_phrasing(self):
        session = make_session("TOWER_DEP")
        session.propose_runway("20R", by="\u767d\u4e91\u653e\u884c")
        out, notes = enforce_message(
            "G-NXWB\uff0c\u5730\u9762\u98ce\u4e0d\u5b9a\uff0c36\u5de6\u8dd1\u9053\uff0c\u53ef\u4ee5\u8d77\u98de\u3002", session)
        self.assertIn("20", out)
        self.assertNotIn("36", out)
        self.assertIn("runway->20R", notes)

    def test_prompt_blocks_expose_authoritative_state(self):
        session = make_session("TOWER_DEP")
        session.assign("squawk", "4231", by="\u767d\u4e91\u653e\u884c")
        session.propose_runway("20R", by="\u767d\u4e91\u653e\u884c")
        session.assign("sid", "VIBOS2", by="\u767d\u4e91\u653e\u884c")
        block = session.state_block()
        self.assertIn("4231", block)
        self.assertIn("20R", block)
        self.assertIn("VIBOS2", block)
        self.assertIn("NEVER contradict", block)

    def test_mutable_fields_can_be_updated(self):
        session = make_session("DEPARTURE")
        session.assign("cleared_altitude", "3000", by="\u767d\u4e91\u5854\u53f0")
        session.assign("cleared_altitude", "6000", by="\u767d\u4e91\u79bb\u573a")
        self.assertEqual(session.get("cleared_altitude"), "6000")


class TelemetryAdvanceTests(unittest.TestCase):
    def test_airborne_advances_to_departure(self):
        session = make_session("TOWER_DEP")
        self.assertEqual(session.observe_telemetry(on_ground=False, altitude=1200, vs=800, groundspeed=120),
                         "DEPARTURE")

    def test_sequence_never_goes_backwards(self):
        session = make_session("DEPARTURE")
        self.assertFalse(session.advance_to("TOWER_DEP"))
        self.assertEqual(session.phase, "DEPARTURE")

    def test_departure_then_center(self):
        session = make_session("DEPARTURE")
        self.assertEqual(session.observe_telemetry(on_ground=False, altitude=19000, vs=0, groundspeed=420),
                         "CENTER")


class ReleaseHandoffTests(unittest.TestCase):
    """终止性许可（放行/起飞/落地）之后必须带上下一个联系人的频率。"""

    def _session(self, phase=None, plan=None):
        session = ATCSession(config={"frequencies": {"Center": "132.500"}},
                             airport_frequency_service=FakeFreqService())
        session.attach({})
        session.load_from_flight_plan(
            plan or {"origin": "ZGGG", "destination": "VHHH", "cruise_alt": 20700},
            callsign="G-NXWB")
        if phase:
            session.advance_to(phase)
        return session

    def test_clearance_without_handoff_gets_ground_frequency(self):
        session = self._session("CLEARANCE")
        out, notes = enforce_message(
            "G-NXWB\uff0c\u653e\u884c\u6709\u6548\u3002\u8dd1\u905320\u53f3\uff0c\u5e94\u7b54\u673a4231\u3002", session)
        self.assertIn("121.750", out)
        self.assertNotIn("\u3002\uff0c", out)          # \u4e0d\u80fd\u51fa\u73b0\u53e0\u53e5\u53f7
        self.assertIn("handoff_freq", notes)

    def test_takeoff_clearance_gets_departure_frequency(self):
        session = self._session("TOWER_DEP")
        session.assign("squawk", "4231", by="\u767d\u4e91\u653e\u884c")
        session.propose_runway("20R", by="\u767d\u4e91\u653e\u884c")
        out, _ = enforce_message(
            "G-NXWB\uff0c\u8dd1\u905320\u53f3\uff0c\u53ef\u4ee5\u8d77\u98de\u3002", session)
        self.assertIn("120.400", out)

    def test_english_release_gets_frequency(self):
        session = self._session("TOWER_DEP")
        out, _ = enforce_message("G-NXWB, cleared for takeoff.", session)
        self.assertIn("120.400", out)
        self.assertIn("contact", out.lower())

    def test_pushback_alone_does_not_hand_off_to_tower(self):
        # \u63a8\u51fa\u4e4b\u540e\u8fd8\u8981\u7ee7\u7eed\u8ddf\u5730\u9762\u8bb2\u8bdd\uff0c\u4e0d\u8be5\u63d0\u524d\u4ea4\u7ed9\u5854\u53f0
        session = self._session("GROUND_DEP")
        out, _ = enforce_message("G-NXWB\uff0c\u5141\u8bb8\u63a8\u51fa\u3002", session)
        self.assertNotIn("118.100", out)

    def test_atis_broadcast_never_gets_a_handoff(self):
        session = self._session()
        out, _ = enforce_message(
            "\u5e7f\u5dde\u767d\u4e91\u673a\u573a\u4fe1\u606f\u4ee3\u53f7Alpha\uff0c\u8dd1\u905302\u53f3\uff0c\u4fee\u6b63\u6d77\u538b1013\u3002",
            session)
        self.assertNotIn("\u8054\u7cfb", out)

    def test_wrong_frequency_for_named_role_is_corrected(self):
        # 说了“联系离场”却拿了塔台的频率，应改成离场的频率
        session = self._session("TOWER_DEP")
        out, _ = enforce_message(
            "G-NXWB\uff0c\u53ef\u4ee5\u8d77\u98de\u3002\u8054\u7cfb\u79bb\u573a 118.100\uff0c\u518d\u89c1\u3002", session)
        self.assertIn("120.400", out)
        self.assertNotIn("118.100", out)


class LogicManagerWiringTests(unittest.TestCase):
    """LogicManager 与 session 的接线。"""

    def _build(self):
        cfg = {"frequencies": {"Center": "132.500"},
               "audio": {"stt_language": "zh"}, "debug": {}}
        svc = FakeFreqService()
        session = ATCSession(cfg, svc)

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
        lm = LogicManager(cfg, io, airport_frequency_service=svc, atc_session=session)
        lm.log_file = "NUL"
        return lm, session, io

    def test_flight_plan_lands_in_shared_context(self):
        from core.context import shared_context
        lm, session, io = self._build()
        lm.on_flight_plan_loaded({"callsign": "G-NXWB", "origin": "ZGGG",
                                  "destination": "VHHH", "cruise_alt": 20700})
        self.assertEqual(shared_context["flight_plan"].get("origin"), "ZGGG")
        self.assertEqual(shared_context["atc_state"]["session"], session.state)

    def test_redirect_reply_has_no_dangling_callsign(self):
        lm, session, io = self._build()
        lm.on_flight_plan_loaded({"origin": "ZGGG", "destination": "VHHH"})
        lm.switch_frequency_context(118.1, source="pilot")
        lm.process_llm_request("\u7533\u8bf7\u8d77\u98de")
        self.assertIn("121.950", io.last)
        self.assertFalse(io.last.startswith("\uff0c"))
        self.assertNotIn("N/A", io.last)

    def test_release_clearance_appends_next_frequency(self):
        lm, session, io = self._build()
        lm.on_flight_plan_loaded({"callsign": "G-NXWB", "origin": "ZGGG",
                                  "destination": "VHHH", "cruise_alt": 20700})
        lm.switch_frequency_context(121.95, source="pilot")
        lm.on_llm_response(
            "G-NXWB\uff0c\u653e\u884c\u6709\u6548\u3002\u8dd1\u905320\u53f3\uff0c\u5e94\u7b54\u673a4231\u3002", None)
        self.assertIn("121.750", io.last)
        self.assertEqual(session.get("runway"), "20R")
        self.assertEqual(session.get("squawk"), "4231")


if __name__ == "__main__":
    unittest.main(verbosity=2)
