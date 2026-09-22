"""阶段三（工作块 C）回归测试：SimConnect AI 交通枚举与 traffic_manager 打通。

不含真实 SimConnect（Linux 环境装不了）：
  - radius clamp / 尾流类别 / dwData 解包：纯函数直接对拍
  - reader：用假 SimConnect 客户端（connect_fn 注入）验证 (req,obj) 双键路由、
    逐字段解包、字符串截断、输出 dict 结构与交通源形态推断
  - traffic_manager：验证结构化 dict 落库、读取器不可用时降级 mock
  - C1 主修回链（docs/fix-plan-c1-traffic-reader.md §P0）：注入假 SimConnect
    模块走真实 _make_traffic_simconnect_class() 子类 + 自建 dispatch 线程，
    验证 CallDispatch → 子类覆写 handle_simobject_event → reader → poll_once
    全链路；SystemExit 兜底；stop() 先停线程；_table 老化剔除。

真实模拟器验证项（MSFS+FSLTL）按计划标注为需实机，见 docs/upgrade-plan-v2 §10
与 docs/fix-plan-c1-traffic-reader.md §P0 验收。

运行：python -m unittest discover -s tests -v
"""
import ctypes
import struct
import sys
import time
import types
import unittest
from types import SimpleNamespace
from unittest import mock

from core.simconnect_traffic import (FIELD_TABLE, SOURCE_LIVE, SOURCE_NONE,
                                     SOURCE_STATIC, SOURCE_UNAVAILABLE,
                                     SimConnectTrafficReader, _decode_record,
                                     compute_radius_m, wake_category_for_type)


class RadiusClampTests(unittest.TestCase):
    def test_default_radius(self):
        self.assertEqual(compute_radius_m({}), 40 * 1852)

    def test_radius_clamped_to_100nm(self):
        # E10：MSFS 上限 200 km（≈108 NM），本实现自紧到 100 NM
        m = compute_radius_m({"traffic": {"traffic_radius_nm": 250}})
        self.assertEqual(m, 100 * 1852)
        self.assertLessEqual(m, 200_000)

    def test_radius_floor_and_bad_value(self):
        self.assertEqual(compute_radius_m({"traffic": {"traffic_radius_nm": 0}}), 185)
        self.assertEqual(compute_radius_m({"traffic": {"traffic_radius_nm": "abc"}}),
                         40 * 1852)

    def test_navdata_fallback_key(self):
        m = compute_radius_m({"navdata": {"traffic_radius_nm": 20}})
        self.assertEqual(m, 20 * 1852)


class WakeCategoryTests(unittest.TestCase):
    def test_known_types(self):
        self.assertEqual(wake_category_for_type("A388"), "J")    # Super
        self.assertEqual(wake_category_for_type("B77W"), "H")    # Heavy
        self.assertEqual(wake_category_for_type("B752"), "H")    # FAA：757 按 Heavy
        self.assertEqual(wake_category_for_type("A320"), "M")
        self.assertEqual(wake_category_for_type("B738"), "M")
        self.assertEqual(wake_category_for_type("C172"), "L")

    def test_prefix_match(self):
        self.assertEqual(wake_category_for_type("B738X"), "M")

    def test_unknown(self):
        self.assertEqual(wake_category_for_type(""), "UNKNOWN")
        self.assertEqual(wake_category_for_type("ZZZZ"), "UNKNOWN")
        self.assertEqual(wake_category_for_type(None), "UNKNOWN")


class DecodeRecordTests(unittest.TestCase):
    def _pack(self):
        """按 FIELD_TABLE 顺序构造一段 dwData 字节。"""
        def s(text):
            raw = text.encode("ascii")
            return raw + b"\x00" * (256 - len(raw))
        parts = [
            struct.pack("<d", 31.1979),   # PLANE LATITUDE
            struct.pack("<d", 121.3360),  # PLANE LONGITUDE
            struct.pack("<d", 1500.0),    # PLANE ALTITUDE (ft)
            struct.pack("<d", 12.5),      # GROUND VELOCITY
            struct.pack("<d", -300.0),    # VERTICAL SPEED
            struct.pack("<d", 90.0),      # HEADING
            struct.pack("<i", 1),         # SIM ON GROUND
            s("CES123"),                 # ATC ID
            s("B738"),                   # ATC TYPE
            s("Boeing 737-800"),         # ATC MODEL
            s("13R"),                    # ASSIGNED RUNWAY
            s("G12"),                    # ASSIGNED PARKING
            s("ZSPD"),                   # CURRENT ICAO
            struct.pack("<i", 0),         # ISIFR
            s("ZBAA"),                   # FROMAIRPORT
            s("ZGGG"),                   # TOAIRPORT
            struct.pack("<d", 240.0),    # ETA
        ]
        return b"".join(parts)

    def test_field_table_order_matches_packing(self):
        total = sum({"float64": 8, "int32": 4, "string256": 256,
                     "string32": 32}[f[2]] for f in FIELD_TABLE)
        self.assertEqual(total, len(self._pack()))

    def test_decode(self):
        values = _decode_record(self._pack(), [f[0] for f in FIELD_TABLE])
        self.assertAlmostEqual(values["PLANE LATITUDE"], 31.1979, places=4)
        self.assertEqual(values["SIM ON GROUND"], 1)
        self.assertEqual(values["ATC ID"], "CES123")
        self.assertEqual(values["ATC TYPE"], "B738")
        self.assertEqual(values["AI TRAFFIC ASSIGNED RUNWAY"], "13R")
        self.assertEqual(values["AI TRAFFIC TOAIRPORT"], "ZGGG")
        self.assertAlmostEqual(values["AI TRAFFIC ETA"], 240.0, places=1)

    def test_truncated_buffer_leaves_none(self):
        values = _decode_record(self._pack()[:100], [f[0] for f in FIELD_TABLE])
        self.assertIsNone(values.get("ATC ID"))
        self.assertIsNotNone(values["PLANE LATITUDE"])


class _FakeDll:
    def __init__(self):
        self.definitions = []
        self.requests = []

    def AddToDataDefinition(self, h, def_id, name, unit, dtype, eps, datum_id):
        self.definitions.append((def_id, name.decode(), unit.decode()))

    def RequestDataOnSimObjectType(self, h, req_id, def_id, radius_m, obj_type):
        self.requests.append((req_id, def_id, radius_m, obj_type))


class _FakeSimConnect:
    """最小假 SimConnect：只实现 reader 用到的接口。"""

    def __init__(self):
        self.dll = _FakeDll()
        self.hSimConnect = 1
        self._def = 100
        self._req = 200

    def new_def_id(self):
        self._def += 1
        return SimpleNamespace(value=self._def)

    def new_request_id(self):
        self._req += 1
        return SimpleNamespace(value=self._req)

    def exit(self):
        pass


def _fake_objdata(request_id, object_id, raw: bytes):
    words = (len(raw) + 3) // 4
    arr = (ctypes.c_uint32 * max(words, 1))()
    ctypes.memmove(arr, raw, len(raw))
    return SimpleNamespace(dwRequestID=request_id, dwObjectID=object_id, dwData=arr)


class ReaderTests(unittest.TestCase):
    def _reader(self, config=None):
        reader = SimConnectTrafficReader(config or {}, connect_fn=_FakeSimConnect)
        self.assertTrue(reader.start())
        return reader

    def test_start_registers_definition(self):
        reader = self._reader()
        names = [name for _id, name, _u in reader._sm.dll.definitions]
        self.assertIn("ATC ID", names)
        self.assertIn("AI TRAFFIC ASSIGNED RUNWAY", names)
        self.assertIn("PLANE LATITUDE", names)

    def test_connect_failure_marks_unavailable(self):
        def _fail():
            raise ConnectionError("Did not find Flight Simulator running.")
        reader = SimConnectTrafficReader({}, connect_fn=_fail)
        self.assertFalse(reader.start())
        self.assertFalse(reader.available)
        self.assertEqual(reader.source_state, SOURCE_UNAVAILABLE)
        self.assertEqual(reader.poll_once(), [])

    def test_single_object_accumulated_per_object_id(self):
        reader = self._reader()
        raw = self._raw(callsign="CES123", on_ground=1, runway="13R")
        reader.handle_simobject_event(_fake_objdata(reader._request_id.value, 7, raw))
        raw2 = self._raw(callsign="CCA981", on_ground=0, runway="")
        reader.handle_simobject_event(_fake_objdata(reader._request_id.value, 9, raw2))
        targets = reader.poll_once()
        by_callsign = {t["callsign"]: t for t in targets}
        self.assertEqual(set(by_callsign), {"CES123", "CCA981"})
        self.assertEqual(by_callsign["CES123"]["assigned_runway"], "13R")
        self.assertEqual(by_callsign["CES123"]["wake_category"], "M")
        self.assertTrue(by_callsign["CES123"]["on_ground"])
        self.assertFalse(by_callsign["CCA981"]["on_ground"])
        self.assertEqual(by_callsign["CES123"]["icao_dest"], "ZGGG")

    def test_request_uses_clamped_radius_and_aircraft_type(self):
        reader = self._reader({"traffic": {"traffic_radius_nm": 250}})
        reader.poll_once()
        _req, _def, radius_m, obj_type = reader._sm.dll.requests[-1]
        self.assertEqual(radius_m, 100 * 1852)
        self.assertEqual(int(obj_type), 2)  # SIMCONNECT_SIMOBJECT_TYPE_AIRCRAFT

    def test_source_state_none_when_empty(self):
        reader = self._reader()
        reader._window_start = time.time() - 61
        reader._refresh_source_state([])
        self.assertEqual(reader.source_state, SOURCE_NONE)

    def test_source_state_live_with_churn_and_eta(self):
        reader = self._reader()
        reader._refresh_source_state([{"ATC ID": "A"}])
        reader._refresh_source_state([{"ATC ID": "A"}, {"ATC ID": "B"}])
        reader._refresh_source_state([{"ATC ID": "B"}, {"ATC ID": "C"}])
        reader._window_start = time.time() - 61
        reader._refresh_source_state([{"ATC ID": "C", "AI TRAFFIC ETA": 120.0}])
        self.assertEqual(reader.source_state, SOURCE_LIVE)

    def test_source_state_static_without_churn(self):
        reader = self._reader()
        reader._refresh_source_state([{"ATC ID": "A"}])
        reader._window_start = time.time() - 61
        reader._refresh_source_state([{"ATC ID": "A"}])
        self.assertEqual(reader.source_state, SOURCE_STATIC)

    @staticmethod
    def _raw(callsign, on_ground, runway):
        def s(text):
            raw = text.encode("ascii")
            return raw + b"\x00" * (256 - len(raw))
        parts = [
            struct.pack("<d", 31.1979),
            struct.pack("<d", 121.3360),
            struct.pack("<d", 1500.0),
            struct.pack("<d", 12.5),
            struct.pack("<d", -300.0),
            struct.pack("<d", 90.0),
            struct.pack("<i", on_ground),
            s(callsign),
            s("B738"),
            s(""),
            s(runway),
            s(""),
            s(""),
            struct.pack("<i", 0),
            s(""),
            s("ZGGG"),
            struct.pack("<d", 0.0),
        ]
        return b"".join(parts)


class TrafficManagerWiringTests(unittest.TestCase):
    def _manager(self):
        from core.traffic_manager import TrafficStateManager
        cfg = {"traffic": {"enabled": True, "msfs_ai_enabled": True},
               "debug": {"mock_traffic_fallback": True}}
        bridge = SimpleNamespace(connected=True, sm=None)
        return TrafficStateManager(cfg, bridge, socketio=None)

    def test_process_ai_object_stores_new_fields(self):
        mgr = self._manager()
        mgr._process_ai_object({
            "callsign": "CES123", "latitude": 31.1, "longitude": 121.3,
            "altitude": 1500, "heading": 90, "airspeed": 12, "vertical_speed": 0,
            "on_ground": True, "aircraft_type": "B738", "wake_category": "M",
            "assigned_runway": "13R", "assigned_parking": "G12",
            "icao_dest": "ZGGG", "eta_s": 240.0,
        })
        ac = mgr.aircraft["CES123"]
        self.assertEqual(ac.aircraft_type, "B738")
        self.assertEqual(ac.wake_category, "M")
        self.assertEqual(ac.assigned_runway, "13R")
        self.assertEqual(ac.icao_dest, "ZGGG")

    def test_reader_unavailable_falls_back_to_mock(self):
        mgr = self._manager()
        # Linux 无 SimConnect：reader.start() 必然失败 → 走 mock 降级
        mgr._scan_traffic()
        self.assertEqual(mgr.traffic_source_state, SOURCE_UNAVAILABLE)
        self.assertGreater(len(mgr.aircraft), 0)
        self.assertTrue(all(ac.wake_category == "UNKNOWN" for ac in mgr.aircraft.values()))

    def test_msfs_ai_disabled_never_creates_reader(self):
        from core.traffic_manager import TrafficStateManager
        cfg = {"traffic": {"enabled": True, "msfs_ai_enabled": False}}
        mgr = TrafficStateManager(cfg, SimpleNamespace(connected=True, sm=None), socketio=None)
        self.assertIsNone(mgr._ensure_traffic_reader())
        self.assertFalse(mgr.msfs_ai_enabled)


class _FakeDispatchDll:
    """模拟 SimConnect.dll：CallDispatch 触发 dispatch proc（真机由 MSFS 驱动）。"""

    def __init__(self, conn):
        self._conn = conn

    def AddToDataDefinition(self, h, def_id, name, unit, dtype, eps, datum_id):
        self._conn.definitions.append((def_id, name.decode(), unit.decode()))

    def RequestDataOnSimObjectType(self, h, req_id, def_id, radius_m, obj_type):
        self._conn.requests.append((req_id, def_id, radius_m, obj_type))

    def CallDispatch(self, h, dispatch_proc, ctx):
        packets = self._conn.pending_packets
        if packets:
            dispatch_proc(packets.pop(0))


class _FakeBaseSimConnect:
    """Stand-in for python-SimConnect 0.4.8（已从 PyPI 核实的行为）：

    - 无后台 dispatch 线程（0.4.8 基类不会自动泵消息）；
    - dispatch proc 运行期动态查找 `self.handle_simobject_event`，
      与 0.4.8 源码 BYTYPE 分发一致（子类覆写/实例属性覆盖均生效）。
    """

    def __init__(self, auto_connect=True):
        self.hSimConnect = 1
        self.definitions = []
        self.requests = []
        self.pending_packets = []
        self.dispatched_default = 0   # 基类默认实现被调用的次数（应为 0）
        self._def = 100
        self._req = 200
        self.dll = _FakeDispatchDll(self)

        def _dispatch_proc(pObjData):
            self.handle_simobject_event(pObjData)

        self.my_dispatch_proc_rd = _dispatch_proc

    def handle_simobject_event(self, ObjData):
        # 基类默认实现只取 definitions[0]，这里只计数证明"没走默认"
        self.dispatched_default += 1

    def new_def_id(self):
        self._def += 1
        return SimpleNamespace(value=self._def)

    def new_request_id(self):
        self._req += 1
        return SimpleNamespace(value=self._req)

    def exit(self):
        self.hSimConnect = 0


class DispatchChainTests(unittest.TestCase):
    """C1 主修收链路（docs/fix-plan-c1-traffic-reader.md §P0/P0-3/P0-5）。

    注入假 SimConnect 模块走真实 `_make_traffic_simconnect_class()` 子类 +
    自建 dispatch 线程，端到端模拟真机链路；MSFS 验证项仍需实机。
    """

    def _reader_with_real_subclass(self):
        from core import simconnect_traffic as mod
        module = types.ModuleType("SimConnect")
        module.SimConnect = _FakeBaseSimConnect
        patched = mock.patch.dict(sys.modules, {"SimConnect": module})
        patched.start()
        self.addCleanup(patched.stop)
        reader = SimConnectTrafficReader(
            {}, traffic_sm_factory=mod._make_traffic_simconnect_class)
        self.assertTrue(reader.start())
        self.addCleanup(reader.stop)
        return reader

    def test_dispatch_thread_delivers_packets_to_poll_once(self):
        reader = self._reader_with_real_subclass()
        sm = reader._sm
        self.assertIsInstance(sm, _FakeBaseSimConnect)
        self.assertIs(sm._traffic_sink, reader)      # start() 已把 reader 挂为 sink
        self.assertIsNotNone(reader._thread)         # 自建 dispatch 线程已自建
        self.assertTrue(reader._thread.is_alive())
        self.assertEqual(reader._thread.name, "SimConnectTrafficDispatch")
        # 队列里放一个 BYTYPE 回包：dispatch 线程 → CallDispatch → 子类覆写 → reader
        raw = ReaderTests._raw(callsign="CES123", on_ground=1, runway="13R")
        sm.pending_packets.append(_fake_objdata(reader._request_id.value, 7, raw))
        targets = []
        deadline = time.time() + 2.0
        while time.time() < deadline:
            targets = reader.poll_once()
            if targets:
                break
            time.sleep(0.05)
        by_callsign = {t["callsign"]: t for t in targets}
        self.assertIn("CES123", by_callsign)
        self.assertEqual(by_callsign["CES123"]["assigned_runway"], "13R")
        self.assertEqual(by_callsign["CES123"]["wake_category"], "M")
        self.assertEqual(sm.dispatched_default, 0)   # 走的是子类覆写而非基类默认

    def test_stop_joins_dispatch_thread(self):
        reader = self._reader_with_real_subclass()
        thread = reader._thread
        self.assertTrue(thread.is_alive())
        reader.stop()
        self.assertFalse(thread.is_alive())
        self.assertIsNone(reader._thread)
        self.assertFalse(reader.available)

    def test_connect_systemexit_becomes_connection_error(self):
        # 0.4.8 connect() 在 MSFS 未运行时 exit(0) → SystemExit（穿透
        # except Exception）；必须降级 available=False 而不是杀死交通扫描线程
        class _ExitingSimConnect:
            def __init__(self, auto_connect=True):
                raise SystemExit(0)

        reader = SimConnectTrafficReader({}, traffic_sm_factory=_ExitingSimConnect)
        self.assertFalse(reader.start())
        self.assertFalse(reader.available)
        self.assertEqual(reader.source_state, SOURCE_UNAVAILABLE)
        self.assertIn("SimConnect exited during connect", reader.last_error or "")
        self.assertIsNone(reader._thread)            # 未启动线程，无泄漏
        self.assertEqual(reader.poll_once(), [])

    def test_stale_objects_evicted_from_table(self):
        # §P0-5：对象离场后不再回包，超过 TTL 的 (req,obj) 条目必须剔除
        reader = SimConnectTrafficReader({}, connect_fn=_FakeSimConnect)
        self.assertTrue(reader.start())
        self.addCleanup(reader.stop)
        raw = ReaderTests._raw(callsign="CES123", on_ground=1, runway="13R")
        reader.handle_simobject_event(_fake_objdata(reader._request_id.value, 7, raw))
        self.assertEqual(len(reader.poll_once()), 1)
        key = (reader._request_id.value, 7)
        reader._table[key]["_last_seen"] = time.time() - reader._TABLE_TTL_SECONDS - 1
        self.assertEqual(reader.poll_once(), [])
        self.assertNotIn(key, reader._table)

    def test_fresh_objects_survive_eviction(self):
        reader = SimConnectTrafficReader({}, connect_fn=_FakeSimConnect)
        self.assertTrue(reader.start())
        self.addCleanup(reader.stop)
        raw = ReaderTests._raw(callsign="CES123", on_ground=0, runway="")
        reader.handle_simobject_event(_fake_objdata(reader._request_id.value, 7, raw))
        targets = reader.poll_once()
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["callsign"], "CES123")
        self.assertIn((reader._request_id.value, 7), reader._table)


if __name__ == "__main__":
    unittest.main(verbosity=2)
