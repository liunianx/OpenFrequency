"""混合自有交通（owned-traffic）P1/P2 回归测试。
见 docs/hybrid-owned-traffic-plan.md §4 P1（注入器）与 P2（状态回灌）。

不含真实 SimConnect / MSFS（Linux 装不了）：
  - 开关 / 配置解析：默认关、max_aircraft 兜底、SimConnect 缺失或 connect
    失败（含 SystemExit 穿透）一律降级 available=False；
  - spawn 全链路：AICreateNonATCAircraft 参数装配 → 自建 dispatch 线程 →
    ASSIGNED_OBJECT_ID 回包 → owned 表 / object 索引（用真 ctypes 结构体
    造回包，走生产 DispatchProc）；
  - 去重（dedup_provider）、max_aircraft 限额、DLL 异常 / 回包 EXCEPTION /
    超时三种失败路径互不影响；
  - 清理：despawn / despawn_all / stop；pending 期间 despawn 的"僵尸回包"
    必须被立刻 AIRemoveObject（防孤儿 SimObject，计划 §8 风险表）；
  - P2：traffic_manager 对命中注入器 object 索引的枚举机标 owned=True。

真实模拟器验证项（MSFS+FSLTL）按计划标注为需实机，见计划 §9。

运行：python -m unittest discover -s tests -v
"""
import ctypes
import sys
import threading
import time
import unittest
from types import SimpleNamespace

from core.owned_traffic_injector import (
    DEFAULT_MAX_AIRCRAFT,
    RECV_ID_ASSIGNED_OBJECT_ID,
    RECV_ID_EXCEPTION,
    SIMCONNECT_DATA_INITPOSITION,
    SIMCONNECT_RECV_ASSIGNED_OBJECT_ID,
    SIMCONNECT_RECV_EXCEPTION,
    SIMCONNECT_RECV_SIMOBJECT_DATA,
    DispatchProc,
    OwnedTrafficInjector,
)

TEST_OBJECT_ID = 4242


def _cint(value):
    """ctypes 整数实例 / Python int → int。

    注：本仓 CI 用的 Python 3.14 上 int(ctypes.c_uint(...)) 会走
    __bytes__ 而抛 ValueError；统一用 .value 取原生值。
    """
    return value.value if hasattr(value, "value") else int(value)


class _LibRecv(ctypes.Structure):
    """模拟库 Enum.py 的 SIMCONNECT_RECV——与生产本地 RECV 布局一致但类
    身份不同。库的 DispatchProc 签名里 POINTER(SIMCONNECT_RECV) 指向这个
    类，这是 P0 第二轮那个 "expected WinFunctionType instead of
    WinFunctionType" 的根源。"""
    _fields_ = [("dwSize", ctypes.c_uint32), ("dwVersion", ctypes.c_uint32),
                ("dwID", ctypes.c_uint32)]


# 模拟库 SimConnectDll.DispatchProc（0.4.26 是 WINFUNCTYPE(None, ...)，
# CI 在 Linux 用 CFUNCTYPE——都与生产模块本地的 DispatchProc 是不同类）
_FakeLibDispatchProc = ctypes.CFUNCTYPE(
    None, ctypes.POINTER(_LibRecv), ctypes.c_uint32, ctypes.c_void_p)


class _StandInLibInitPos(ctypes.Structure):
    """站在"库自己的 SIMCONNECT_DATA_INITPOSITION"的位置（P0 教训）。

    与生产模块本地定义**布局完全相同但类身份不同**——真实 argtypes 校验
    只认库那个类对象，本地类会 ArgumentError。测试经 `initpos_class=`
    注入本类，验证生产代码用的是注入的库类而不是本地类。
    """
    _fields_ = [
        ("Latitude", ctypes.c_double),
        ("Longitude", ctypes.c_double),
        ("Altitude", ctypes.c_double),
        ("Pitch", ctypes.c_double),
        ("Bank", ctypes.c_double),
        ("Heading", ctypes.c_double),
        ("OnGround", ctypes.c_uint32),
        ("Airspeed", ctypes.c_uint32),
    ]


class _LibDataRequestId(ctypes.c_uint32):
    """复刻库 CtypesEnum 的 from_param = int(obj) 行为。

    它会暴露"传 ctypes 整数实例"的坑：Python 3.14 上 int(DWORD实例) 抛
    ValueError——所以生产代码一律传纯 int。
    """

    @classmethod
    def from_param(cls, obj):
        return int(obj)


class _Fn:
    """模拟 cdll 函数指针：argtypes/restype 可重写，调用时按 argtypes 逐
    参数 from_param 校验（复刻 ctypes 对声明了 argtypes 的外部函数的
    ArgumentError 行为——P0 两次翻车都发生在这里），通过后用原始参数调 impl。
    """

    def __init__(self, impl, argtypes):
        self._impl = impl
        self.argtypes = list(argtypes)
        self.restype = None
        self.calls = []

    def __call__(self, *args):
        if self.argtypes:
            for i, (arg, argtype) in enumerate(
                    zip(args, self.argtypes), start=1):
                try:
                    argtype.from_param(arg)
                except Exception as e:
                    raise ctypes.ArgumentError(
                        f"argument {i}: {type(e).__name__}: {e}")
        self.calls.append(args)
        return self._impl(*args)


class _FakeDll:
    """复刻 python-SimConnect Attributes.py 的关键 argtypes 行为。

    library="legacy"（0.4.8）：AICreateNonATCAircraft 的 title/tail 被库
    误声明为 c_double——不修连 bytes 都传不进（P0 教训 #2）。
    library="modern"（≥0.4.9）：c_char_p 已修正，但 InitPos 仍必须是库
    自己的类（P0 教训 #1，用户实机报的 argument 4 错）。
    两种布局下生产代码都必须能调用成功（start() 里修 argtypes + 用库类）。
    """

    def __init__(self, conn, initpos_cls, library="legacy"):
        self._conn = conn
        self._initpos_cls = initpos_cls
        req_id = _LibDataRequestId
        if library == "legacy":
            create_argtypes = [ctypes.c_void_p, ctypes.c_double,
                               ctypes.c_double, initpos_cls, req_id]
        else:
            create_argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                               ctypes.c_char_p, initpos_cls, req_id]
        self.AICreateNonATCAircraft = _Fn(self._ai_create, create_argtypes)
        # ParkedATC：[HANDLE, c_char_p, c_char_p, c_char_p(airport), CtypesEnum]
        # ——库该函数 argtypes 本就正确（P0-12 A 路径主力）
        self.AICreateParkedATCAircraft = _Fn(
            self._ai_create_parked,
            [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
             ctypes.c_char_p, req_id])
        # AISetAircraftFlightPlan：[HANDLE, DWORD, c_char_p, CtypesEnum]
        self.AISetAircraftFlightPlan = _Fn(
            self._ai_set_plan,
            [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, req_id])
        # AIRemoveObject：[HANDLE, DWORD(SIMCONNECT_OBJECT_ID), CtypesEnum]
        self.AIRemoveObject = _Fn(self._ai_remove,
                                  [ctypes.c_void_p, ctypes.c_uint32, req_id])
        self.GetLastSentPacketID = self._get_last_sent_packet_id
        # CallDispatch 只认库自己的 DispatchProc 类型（P0 第二轮教训）
        self.CallDispatch = _Fn(
            self._call_dispatch,
            [ctypes.c_void_p, type(conn.my_dispatch_proc_rd), ctypes.c_void_p])

    def _ai_create(self, h, title, tail, initpos, req_id):
        self._conn.created.append({
            "h": h,
            "title": bytes(title).decode("ascii"),
            "tail": bytes(tail).decode("ascii"),
            "lat": initpos.Latitude,
            "lon": initpos.Longitude,
            "alt": initpos.Altitude,
            "hdg": initpos.Heading,
            "on_ground": initpos.OnGround,
            "airspeed": initpos.Airspeed,
            "req": _cint(req_id),
            "initpos_is_lib_cls": isinstance(initpos, self._initpos_cls),
        })
        if self._conn.fail_create:
            raise OSError("simulated DLL failure")
        request_id = _cint(req_id)
        # P0 probe 实测：异常回包按"发送包 id"关联（非 request id）。
        # 每个 AI 创建调用计为一个发送包，GetLastSentPacketID 返回它。
        self._conn.packet_counter += 1
        packet_id = self._conn.packet_counter
        if self._conn.exception_code is not None:
            send_id = packet_id if self._conn.exception_uses_packet_id \
                else request_id
            self._conn.queue_exception(send_id, self._conn.exception_code)
            if self._conn.exception_once:
                self._conn.exception_code = None   # 只失败一次
        elif self._conn.assign_object_id is not None:
            self._conn.queue_assigned(request_id, self._conn.assign_object_id)
        return 0  # S_OK

    def _ai_create_parked(self, h, title, tail, airport, req_id):
        """ParkedATC：按机场停机位创建（A 路径，P0-12 实测自主滑行）。"""
        self._conn.parked_created.append({
            "title": bytes(title).decode("ascii"),
            "tail": bytes(tail).decode("ascii"),
            "airport": bytes(airport).decode("ascii"),
            "req": _cint(req_id),
        })
        if self._conn.fail_create:
            raise OSError("simulated DLL failure")
        self._conn.packet_counter += 1
        if self._conn.assign_object_id is not None:
            self._conn.queue_assigned(_cint(req_id),
                                      self._conn.assign_object_id)
        return 0

    def _ai_set_plan(self, h, obj_id, path, req_id):
        self._conn.plans.append((_cint(obj_id), bytes(path).decode("ascii")))
        return 0

    def _ai_remove(self, h, obj_id, req_id):
        self._conn.removed.append(_cint(obj_id))
        return 0

    def AddToDataDefinition(self, h, def_id, name, unit, dtype, eps, datum_id):
        # P0-5 教训：datumID 必须 SIMCONNECT_UNUSED；0 会让多个 datum
        # 全挂 client data 0 → DUPLICATE_ID 连环拒。这里记录供断言。
        self._conn.definitions.append(
            (_cint(def_id), bytes(name).decode("ascii", "ignore"),
             _cint(datum_id)))
        return 0

    def RequestDataOnSimObject(self, h, req_id, def_id, obj_id, period,
                               flags, origin, interval, limit):
        rid = _cint(req_id)
        self._conn.data_requests.append(rid)
        # 首次数据请求当作 title 查询自动应答；之后（位置轮询）由测试
        # 显式 queue_position 给包，避免与 title 流混淆。
        if self._conn.user_title and not self._conn.title_auto_answered:
            self._conn.title_auto_answered = True
            self._conn.queue_user_data(rid, self._conn.user_title)
        return 0

    def _get_last_sent_packet_id(self, h, out_ptr):
        # SimConnect_GetLastSentPacketID(HANDLE, DWORD*)——返回最近发包 id
        ctypes.cast(out_ptr, ctypes.POINTER(ctypes.c_uint32))[0] = \
            self._conn.packet_counter
        return 0

    def _call_dispatch(self, h, dispatch_proc, ctx):
        while self._conn.pending_packets:
            rec = self._conn.pending_packets.pop(0)
            ptr = ctypes.cast(ctypes.byref(rec),
                              ctypes.POINTER(_LibRecv))
            dispatch_proc(ptr, rec.dwSize, None)


class _FakeSimConnect:
    """最小假 SimConnect：只实现注入器用到的接口。

    initpos_cls/library 用于切换"库形态"（legacy bug / modern fixed），
    见 _FakeDll 文档。
    """

    def __init__(self, auto_connect=True, initpos_cls=None, library="legacy"):
        self.hSimConnect = 0x7E11
        self.created = []
        self.removed = []
        self.pending_packets = []
        self.fail_create = False
        self.assign_object_id = TEST_OBJECT_ID   # None = 不回包（制造超时）
        self.exception_code = None               # 设置则回 EXCEPTION 包
        # 发包计数（SimConnect_GetLastSentPacketID 语义）：异常按发包 id 关联
        self.packet_counter = 0
        # False=模拟旧版 SimConnect：异常回包的 sendID 位直接给 request id
        # （用于测试"拿不到发包 id 时按 request id 关联"的回退路径）
        self.exception_uses_packet_id = True
        # True=异常只发一次（之后清空）——模拟"第一次创建失败、兜底重试成功"
        self.exception_once = False
        # 用户飞机上下文（TITLE）查询的应答；None=不回包
        self.user_title = None
        self.definitions = []       # (def_id, datum name, datum_id)
        self.data_requests = []     # RequestDataOnSimObject 的 req id
        self.parked_created = []    # ParkedATC 调用记录
        self.plans = []             # AISetAircraftFlightPlan 调用记录
        self.title_auto_answered = False   # title 只自动应答第一次
        # 库模型自带的 dispatch proc 实例——既作"库类型"的载体，也作
        # 生产代码 _make_dispatch_proc 取 type() 的源头（P0 第三轮教训）。
        # my_dispatch_proc 是库原始处理方法，my_dispatch_proc_rd 是其
        # ctypes 包装（与真库同构）；_install_dispatch_chain 会包装前者
        # 并重建后者（P0 probe 实测的后台泵抢包修复）。
        def _lib_default_proc(pData, cbData, pContext):
            pass
        self.my_dispatch_proc = _lib_default_proc
        self.my_dispatch_proc_rd = _FakeLibDispatchProc(self.my_dispatch_proc)
        self.dll = _FakeDll(self, initpos_cls or SIMCONNECT_DATA_INITPOSITION,
                            library=library)

    def new_def_id(self):
        # 库实例自带方法：分配一个 data definition id（真机为 Enum 成员）
        self._def_counter = getattr(self, "_def_counter", 100) + 1
        return SimpleNamespace(value=self._def_counter)

    def queue_assigned(self, request_id, object_id):
        self.pending_packets.append(SIMCONNECT_RECV_ASSIGNED_OBJECT_ID(
            dwSize=ctypes.sizeof(SIMCONNECT_RECV_ASSIGNED_OBJECT_ID),
            dwVersion=0x2, dwID=RECV_ID_ASSIGNED_OBJECT_ID,
            dwRequestID=request_id, dwObjectID=object_id))

    def queue_exception(self, send_id, code):
        self.pending_packets.append(SIMCONNECT_RECV_EXCEPTION(
            dwSize=ctypes.sizeof(SIMCONNECT_RECV_EXCEPTION),
            dwVersion=0x2, dwID=RECV_ID_EXCEPTION,
            dwException=code, dwSendID=0, dwIndex=0,
            UNKNOWN_SENDID=send_id))

    def queue_position(self, request_id, lat, lon, alt):
        """模拟自有 AI 的位置回包：dwData = 3×float64(LAT/LON/ALT)。"""
        block = (ctypes.c_double * 3)(lat, lon, alt)
        arr = (ctypes.c_uint32 * 8192)()
        ctypes.memmove(arr, block, 24)
        self.pending_packets.append(SIMCONNECT_RECV_SIMOBJECT_DATA(
            dwSize=ctypes.sizeof(SIMCONNECT_RECV_SIMOBJECT_DATA),
            dwVersion=0x2, dwID=8,
            dwRequestID=request_id, dwObjectID=0,
            dwDefineID=1, dwFlags=0,
            dwentrynumber=0, dwoutof=1, dwDefineCount=1,
            dwData=arr))

    def queue_user_data(self, request_id, title):
        """模拟 SIMOBJECT_DATA 回包：dwData[0:256] = title(string256)。"""
        raw = title.encode("ascii", "ignore")
        arr = (ctypes.c_uint32 * 8192)()
        ctypes.memmove(arr, raw, len(raw))
        self.pending_packets.append(SIMCONNECT_RECV_SIMOBJECT_DATA(
            dwSize=ctypes.sizeof(SIMCONNECT_RECV_SIMOBJECT_DATA),
            dwVersion=0x2, dwID=8,
            dwRequestID=request_id, dwObjectID=0,
            dwDefineID=1, dwFlags=0,
            dwentrynumber=0, dwoutof=1, dwDefineCount=1,
            dwData=arr))

    def exit(self):
        self.hSimConnect = 0


class ConfigTests(unittest.TestCase):
    """开关 / 配置解析 / 连接失败的降级路径。"""

    def test_disabled_by_default(self):
        # 计划 §5：owned.enabled 默认 false = 完全关闭，行为等同升级前
        inj = OwnedTrafficInjector({})
        self.assertFalse(inj.enabled)
        self.assertFalse(inj.start())
        self.assertEqual(inj.last_error, "traffic.owned.enabled=false")
        self.assertIsNone(inj.spawn({"callsign": "OF001"}))

    def test_max_aircraft_default_and_bad_value(self):
        self.assertEqual(OwnedTrafficInjector({}).max_aircraft, DEFAULT_MAX_AIRCRAFT)
        self.assertEqual(OwnedTrafficInjector({"traffic": {"owned": {
            "max_aircraft": 4}}}).max_aircraft, 4)
        self.assertEqual(OwnedTrafficInjector({"traffic": {"owned": {
            "max_aircraft": "abc"}}}).max_aircraft, DEFAULT_MAX_AIRCRAFT)
        self.assertEqual(OwnedTrafficInjector({"traffic": {"owned": {
            "max_aircraft": -2}}}).max_aircraft, 0)

    def test_connect_failure_degrades(self):
        def _fail():
            raise ConnectionError("Did not find Flight Simulator running.")
        inj = OwnedTrafficInjector({"traffic": {"owned": {"enabled": True}}},
                                   connect_fn=_fail)
        self.assertFalse(inj.start())
        self.assertFalse(inj.available)
        self.assertIn("Flight Simulator", inj.last_error)
        self.assertIsNone(inj.spawn({"callsign": "OF001"}))

    def test_connect_systemexit_degrades(self):
        # 0.4.8 connect() 在 MSFS 未运行时 exit(0) → SystemExit 穿透
        # except Exception；必须降级而不是杀死调用方线程（与读通道同坑）
        def _exit():
            raise SystemExit(0)
        inj = OwnedTrafficInjector({"traffic": {"owned": {"enabled": True}}},
                                   connect_fn=_exit)
        self.assertFalse(inj.start())
        self.assertIn("SimConnect exited during connect", inj.last_error or "")
        self.assertIsNone(inj._thread)
        self.assertIsNone(inj.spawn({"callsign": "OF001"}))

    def test_start_degrades_when_dll_missing_capability(self):
        class _NoCapDll:
            def __init__(self):
                pass
        conn = _FakeSimConnect()
        conn.dll = _NoCapDll()
        inj = OwnedTrafficInjector({"traffic": {"owned": {"enabled": True}}},
                                   connect_fn=lambda: conn)
        self.assertFalse(inj.start())
        self.assertIn("AICreateNonATCAircraft", inj.last_error or "")

    def test_stop_without_start_is_safe(self):
        inj = OwnedTrafficInjector({"traffic": {"owned": {"enabled": True}}},
                                   connect_fn=_FakeSimConnect)
        inj.stop()   # 不应抛


class SpawnTests(unittest.TestCase):
    """spawn 全链路：参数装配 → dispatch 线程 → 回包 → 表格登记。"""

    def _injector(self, owned_cfg=None, dedup_provider=None):
        cfg = {"traffic": {"owned": {"enabled": True, **(owned_cfg or {})}}}
        inj = OwnedTrafficInjector(
            cfg,
            connect_fn=lambda: _FakeSimConnect(initpos_cls=_StandInLibInitPos),
            dedup_provider=dedup_provider,
            initpos_class=_StandInLibInitPos)   # 模拟"库类已解析"
        self.assertTrue(inj.start())
        self.addCleanup(inj.stop)
        return inj

    def test_spawn_full_chain(self):
        inj = self._injector()
        sm = inj._sm
        owned_id = inj.spawn({
            "model_title": "Airbus A320 Neo Asobo",
            "callsign": "OF001", "tail_number": "OF001",
            "latitude": 40.6413, "longitude": -73.7781,
            "altitude_ft": 13, "heading": 310, "on_ground": True,
            "airspeed_kt": 0,
        })
        self.assertEqual(owned_id, "owned-1")
        # DLL 收到装配好的参数
        self.assertEqual(len(sm.created), 1)
        call = sm.created[0]
        self.assertEqual(call["title"], "Airbus A320 Neo Asobo")
        self.assertEqual(call["tail"], "OF001")
        self.assertAlmostEqual(call["lat"], 40.6413, places=4)
        self.assertAlmostEqual(call["lon"], -73.7781, places=4)
        self.assertEqual(call["alt"], 13.0)
        self.assertEqual(call["hdg"], 310.0)
        self.assertEqual(call["on_ground"], 1)
        self.assertEqual(call["airspeed"], 0)
        # 回包经 dispatch 线程落表
        rec = inj.get_owned(owned_id)
        self.assertEqual(rec["state"], "active")
        self.assertEqual(rec["object_id"], TEST_OBJECT_ID)
        self.assertEqual(rec["callsign"], "OF001")
        self.assertEqual(inj.owned_object_index(),
                         {TEST_OBJECT_ID: "owned-1"})
        listing = inj.list_owned()
        self.assertEqual(len(listing), 1)
        self.assertTrue(listing[0]["owned"])
        self.assertNotIn("spec", listing[0])      # 快照不含原始 spec

    def test_spawn_empty_spec_uses_safe_defaults(self):
        inj = self._injector()
        owned_id = inj.spawn({})
        self.assertEqual(owned_id, "owned-1")
        call = inj._sm.created[0]
        self.assertEqual(call["title"], "")       # 空=默认 AI 机（计划 §6 MVP）
        self.assertEqual(call["tail"], "OF000")
        self.assertEqual(call["on_ground"], 1)
        self.assertEqual(call["airspeed"], 0)

    def test_dedup_appends_suffix_for_taken_callsign(self):
        # 计划 §1 硬约束：避开 FSLTL 已占用呼号，防视觉重影
        inj = self._injector(dedup_provider=lambda: ["OF001", "OF001-2"])
        inj.spawn({"callsign": "OF001"})
        rec = inj.get_owned("owned-1")
        self.assertEqual(rec["callsign"], "OF001-3")

    def test_dedup_provider_error_does_not_block_spawn(self):
        def _broken():
            raise RuntimeError("reader gone")
        inj = self._injector(dedup_provider=_broken)
        self.assertEqual(inj.spawn({"callsign": "OF001"}), "owned-1")
        self.assertEqual(inj.get_owned("owned-1")["callsign"], "OF001")

    def test_max_aircraft_cap(self):
        inj = self._injector({"max_aircraft": 1})
        self.assertEqual(inj.spawn({"callsign": "OF001"}), "owned-1")
        self.assertIsNone(inj.spawn({"callsign": "OF002"}))

    def test_dll_exception_marks_failed_and_others_unaffected(self):
        inj = self._injector({"max_aircraft": 2})
        inj._sm.fail_create = True
        self.assertIsNone(inj.spawn({"callsign": "OF001"}))
        rec = inj.get_owned("owned-1")
        self.assertEqual(rec["state"], "failed")
        self.assertIn("simulated DLL failure", rec["error"])
        self.assertEqual(inj.owned_object_index(), {})
        # 失败不占额度、不影响后续 spawn
        inj._sm.fail_create = False
        self.assertEqual(inj.spawn({"callsign": "OF002"}), "owned-2")
        self.assertEqual(inj.get_owned("owned-2")["state"], "active")

    def test_simconnect_exception_recv_marks_failed(self):
        inj = self._injector()
        inj._sm.exception_code = 17    # 任意 SIMCONNECT_EXCEPTION 值
        self.assertIsNone(inj.spawn({"callsign": "OF001"}))
        rec = inj.get_owned("owned-1")
        self.assertEqual(rec["state"], "failed")
        self.assertIn("exception 17", rec["error"])

    def test_spawn_timeout_marks_failed(self):
        inj = self._injector()
        inj._sm.assign_object_id = None      # 永远不回包
        self.assertIsNone(inj.spawn({"callsign": "OF001"}, timeout=0.2))
        rec = inj.get_owned("owned-1")
        self.assertEqual(rec["state"], "failed")
        self.assertIn("timed out", rec["error"])

    def test_unique_request_ids_per_instance(self):
        inj = self._injector({"max_aircraft": 3})
        inj.spawn({"callsign": "OF001"})
        inj.spawn({"callsign": "OF002"})
        reqs = [c["req"] for c in inj._sm.created]
        self.assertEqual(len(reqs), 2)
        self.assertNotEqual(reqs[0], reqs[1])


class DespawnTests(unittest.TestCase):
    """清理路径：despawn / despawn_all / stop / 僵尸回包。"""

    def _injector(self, owned_cfg=None):
        cfg = {"traffic": {"owned": {"enabled": True, **(owned_cfg or {})}}}
        inj = OwnedTrafficInjector(
            cfg,
            connect_fn=lambda: _FakeSimConnect(initpos_cls=_StandInLibInitPos),
            initpos_class=_StandInLibInitPos)
        self.assertTrue(inj.start())
        self.addCleanup(inj.stop)
        return inj

    def _spawn_one(self, inj):
        owned_id = inj.spawn({"callsign": "OF001"})
        self.assertIsNotNone(owned_id)
        return owned_id

    def test_despawn_removes_via_dll_and_local_tables(self):
        inj = self._injector()
        self._spawn_one(inj)
        self.assertTrue(inj.despawn("owned-1"))
        self.assertEqual(inj._sm.removed, [TEST_OBJECT_ID])
        self.assertEqual(inj.owned_object_index(), {})
        self.assertEqual(inj.list_owned(), [])
        self.assertIsNone(inj.get_owned("owned-1"))

    def test_despawn_unknown_id_is_noop(self):
        inj = self._injector()
        self.assertFalse(inj.despawn("owned-999"))
        self.assertEqual(inj._sm.removed, [])

    def test_despawn_all_and_stop(self):
        inj = self._injector({"max_aircraft": 2})
        sm = inj._sm                     # stop() 会清空 inj._sm，先持引用
        self._spawn_one(inj)
        inj.spawn({"callsign": "OF002"})
        # stop() 必须先 despawn_all（进程退出/场景切换/开关关闭全清）
        inj.stop()
        self.assertEqual(sm.removed, [TEST_OBJECT_ID, TEST_OBJECT_ID])
        self.assertEqual(inj.owned_object_index(), {})
        self.assertIsNone(inj._thread)

    def test_stop_joins_dispatch_thread(self):
        inj = self._injector()
        thread = inj._thread
        self.assertTrue(thread.is_alive())
        self.assertEqual(thread.name, "OwnedTrafficDispatch")
        inj.stop()
        self.assertFalse(thread.is_alive())
        self.assertIsNone(inj._thread)

    def test_despawn_during_pending_late_recv_is_removed(self):
        """pending 期间 despawn，晚到的 ASSIGNED_OBJECT_ID 必须被立刻
        移除——否则飞机已创建但本地失联，成为孤儿 SimObject（计划 §8）。"""
        inj = self._injector()
        sm = inj._sm
        sm.assign_object_id = None            # 先不回包，保持 pending
        result = {}

        def _spawn():
            result["owned_id"] = inj.spawn({"callsign": "OF001"}, timeout=5.0)

        thread = threading.Thread(target=_spawn, daemon=True)
        thread.start()
        deadline = time.time() + 2.0
        while time.time() < deadline and not sm.created:
            time.sleep(0.02)
        self.assertTrue(sm.created)           # 创建请求已到 DLL
        self.assertTrue(inj.despawn("owned-1"))
        thread.join(timeout=2.0)
        self.assertIsNone(result.get("owned_id"))
        # 现在补一个迟到的回包：dispatch 线程应触发 AIRemoveObject
        sm.queue_assigned(sm.created[-1]["req"], TEST_OBJECT_ID)
        deadline = time.time() + 2.0
        while time.time() < deadline and TEST_OBJECT_ID not in sm.removed:
            time.sleep(0.02)
        self.assertEqual(sm.removed, [TEST_OBJECT_ID])
        self.assertEqual(inj.owned_object_index(), {})

    def test_dispatch_thread_delivers_assigned_packet(self):
        """自建 dispatch 线程真的在泵 CallDispatch（0.4.8 基类没有后台线程）。"""
        inj = self._injector()
        sm = inj._sm
        sm.assign_object_id = None            # 先挂着，手动灌包验证线程存活
        # 直接往待派发队列放包，不等 spawn：验证线程自行驱动回包处理
        result = {}

        def _spawn():
            result["owned_id"] = inj.spawn({"callsign": "OF001"}, timeout=2.0)

        thread = threading.Thread(target=_spawn, daemon=True)
        thread.start()
        deadline = time.time() + 2.0
        while time.time() < deadline and not sm.created:
            time.sleep(0.02)
        sm.queue_assigned(sm.created[-1]["req"], TEST_OBJECT_ID)
        thread.join(timeout=3.0)
        self.assertEqual(result.get("owned_id"), "owned-1")


class LibraryArgtypesRegressionTests(unittest.TestCase):
    """P0 实测回归（2026-09-22 实机两次翻车，见计划 §9）：

    失败模式 1（用户实机报的 argument 4）：库 argtypes 要求 InitPos 是
    **库自己的** SIMCONNECT_DATA_INITPOSITION 类——本地同名类
    ArgumentError（expected X instance instead of X，极具迷惑性）。
    失败模式 2（requirements 锁定的 0.4.8）：title/tail 被误声明为
    c_double——bytes 字符串直接 ArgumentError。
    生产代码对两种库形态都必须调用成功（start() 修 argtypes + 库类构造）。
    """

    def _injector(self, library="legacy"):
        cfg = {"traffic": {"owned": {"enabled": True}}}
        inj = OwnedTrafficInjector(
            cfg,
            connect_fn=lambda: _FakeSimConnect(
                initpos_cls=_StandInLibInitPos, library=library),
            initpos_class=_StandInLibInitPos)
        self.assertTrue(inj.start())
        self.addCleanup(inj.stop)
        return inj

    def test_argtypes_repaired_to_sdk_signature(self):
        inj = self._injector("legacy")   # 0.4.8 bug 布局起步
        argtypes = inj._sm.dll.AICreateNonATCAircraft.argtypes
        self.assertEqual(argtypes[0], ctypes.c_void_p)
        self.assertEqual(argtypes[1], ctypes.c_char_p)   # szContainerTitle
        self.assertEqual(argtypes[2], ctypes.c_char_p)   # szTailNumber
        self.assertIs(argtypes[3], _StandInLibInitPos)   # 库类身份
        self.assertEqual(argtypes[4], ctypes.c_uint32)   # RequestID

    def test_legacy_buggy_layout_rejects_bytes_before_repair(self):
        # 固化 0.4.8 的坑：不修 argtypes 时 bytes 过不了 c_double
        conn = _FakeSimConnect(initpos_cls=_StandInLibInitPos, library="legacy")
        with self.assertRaises(ctypes.ArgumentError):
            conn.dll.AICreateNonATCAircraft(
                0x7E11, b"Airbus A320 Neo Asobo", b"OF001",
                _StandInLibInitPos(), 36865)

    def test_modern_library_enforces_initpos_class_identity(self):
        # 固化用户实机的坑：modern 库里本地同名类过不了身份校验
        conn = _FakeSimConnect(initpos_cls=_StandInLibInitPos, library="modern")
        with self.assertRaises(ctypes.ArgumentError):
            conn.dll.AICreateNonATCAircraft(
                0x7E11, b"Airbus A320 Neo Asobo", b"OF001",
                SIMCONNECT_DATA_INITPOSITION(),  # 生产本地类，非库类
                36865)

    def test_spawn_against_legacy_library(self):
        inj = self._injector("legacy")
        owned_id = inj.spawn({"callsign": "OF001"})
        self.assertEqual(owned_id, "owned-1")
        call = inj._sm.created[0]
        self.assertTrue(call["initpos_is_lib_cls"])
        self.assertEqual(call["title"], "")   # 空 title = 默认 AI 机

    def test_spawn_against_modern_library(self):
        inj = self._injector("modern")
        owned_id = inj.spawn({"model_title": "Airbus A320 Neo Asobo",
                              "callsign": "OF001"})
        self.assertEqual(owned_id, "owned-1")
        call = inj._sm.created[0]
        self.assertTrue(call["initpos_is_lib_cls"])
        self.assertEqual(call["title"], "Airbus A320 Neo Asobo")

    def test_spawn_uses_injected_library_class_not_local(self):
        inj = self._injector("modern")
        inj.spawn({"callsign": "OF001"})
        initpos_arg = inj._sm.dll.AICreateNonATCAircraft.calls[-1][3]
        self.assertIsInstance(initpos_arg, _StandInLibInitPos)
        # 本地同名类与库类布局一致但身份不同——曾让实机 ArgumentError
        self.assertNotIsInstance(initpos_arg, SIMCONNECT_DATA_INITPOSITION)

    def test_dispatch_proc_uses_library_type(self):
        # P0 第二轮：CallDispatch 第二参只认库自己的 DispatchProc 类型
        inj = self._injector("modern")
        fake = inj._sm
        self.assertIsInstance(inj._dispatch_proc,
                              type(fake.my_dispatch_proc_rd))
        self.assertNotIsInstance(inj._dispatch_proc, DispatchProc)
        # 走完整 dispatch 链路即证明类型被 CallDispatch 的 argtypes 接受
        self.assertEqual(inj.spawn({"callsign": "OF001"}), "owned-1")

    def test_module_local_proc_type_rejected_by_call_dispatch(self):
        # 固化第三坑：本地同款签名的 DispatchProc 过不了 argtypes 身份校验
        fake = _FakeSimConnect(initpos_cls=_StandInLibInitPos)
        local_proc = DispatchProc(lambda pData, cbData, pContext: None)
        with self.assertRaises(ctypes.ArgumentError):
            fake.dll.CallDispatch(fake.hSimConnect, local_proc, None)

    def test_dispatch_chain_covers_library_background_pump(self):
        """P0 probe 实测：新版库起后台 timerThread 泵 my_dispatch_proc_rd，
        与自建泵竞争收包——ASSIGNED_OBJECT_ID 可能被库线程赢走（库只塞环境
        变量），injector 侧永远等不到。修复：包装库的 my_dispatch_proc 并用
        库类型重建 my_dispatch_proc_rd，两条路径都过我们的处理器。"""
        inj = self._injector("modern")
        fake = inj._sm
        # 连接后库的 dispatch proc 应已被替换为包装版（类型仍是库的）
        self.assertIsInstance(fake.my_dispatch_proc_rd,
                              type(fake.my_dispatch_proc_rd))
        # 先制造一个在途 spawn（不回包），再从库路径灌 EXCEPTION
        fake.assign_object_id = None
        import threading
        result = {}

        def _spawn():
            result["id"] = inj.spawn({"callsign": "ZZ001"}, timeout=3.0)

        thread = threading.Thread(target=_spawn, daemon=True)
        thread.start()
        deadline = time.time() + 2.0
        while time.time() < deadline and not fake.created:
            time.sleep(0.02)
        self.assertTrue(fake.created)
        req_id = fake.created[-1]["req"]
        # 模拟库的后台线程通过 my_dispatch_proc_rd 投递（我们的泵不参与）。
        # P0 probe 实测：异常按"发送包 id"（非 request id）关联。
        rec = SIMCONNECT_RECV_EXCEPTION(
            dwSize=ctypes.sizeof(SIMCONNECT_RECV_EXCEPTION),
            dwVersion=0x2, dwID=RECV_ID_EXCEPTION,
            dwException=22, dwSendID=0, dwIndex=0,
            UNKNOWN_SENDID=fake.packet_counter)
        ptr = ctypes.cast(ctypes.byref(rec), ctypes.POINTER(_LibRecv))
        fake.my_dispatch_proc_rd(ptr, rec.dwSize, None)
        thread.join(timeout=3.0)
        self.assertIsNone(result.get("id"))
        record = inj.get_owned("owned-1")
        self.assertEqual(record["state"], "failed")
        self.assertIn("exception 22", record["error"])

    def test_exception_falls_back_to_request_id_when_no_packet_id(self):
        """GetLastSentPacketID 不可用（旧版库/替身）时，异常按 request id
        关联——两条路径都要能快速失败，不能干等超时。"""
        inj = self._injector("modern")
        inj._sm.dll.GetLastSentPacketID = None   # 模拟旧版库无此函数
        inj._sm.exception_uses_packet_id = False  # 模拟旧版 SimConnect：sendID=request id
        inj._sm.exception_code = 17
        self.assertIsNone(inj.spawn({"callsign": "OF001"}))
        record = inj.get_owned("owned-1")
        self.assertEqual(record["state"], "failed")
        self.assertIn("exception 17", record["error"])

    def test_spawn_falls_back_to_user_aircraft_title(self):
        """P0-7/8 MSFS2024 实测兜底：创建被 34（OBJECT_CONTAINER）拒绝后，
        用用户当前飞机 title 重试一次并成功。"""
        inj = self._injector("modern")
        fake = inj._sm
        fake.exception_code = 34                  # 第一次：容器不存在
        fake.exception_once = True                # 只失败第一次
        fake.user_title = "A350-900 (Default Cabin)"
        owned_id = inj.spawn({"model_title": "Airbus A320 Neo Asobo",
                              "callsign": "OF001"})
        self.assertEqual(owned_id, "owned-2")     # owned-1 失败，兜底 owned-2
        rec = inj.get_owned("owned-2")
        self.assertEqual(rec["state"], "active")
        self.assertEqual(rec["spec"]["model_title"], "A350-900 (Default Cabin)")
        self.assertEqual(rec["object_id"], TEST_OBJECT_ID)
        # 第二次 DLL 创建用的就是兜底 title
        self.assertEqual(fake.created[-1]["title"], "A350-900 (Default Cabin)")
        self.assertEqual(len(fake.data_requests), 1)   # 查过一次 title

    def test_spawn_no_fallback_when_user_title_unavailable(self):
        inj = self._injector("modern")
        fake = inj._sm
        fake.exception_code = 34
        fake.exception_once = True
        fake.user_title = None                    # 拿不到用户 title
        self.assertIsNone(inj.spawn({"callsign": "OF001"}))
        self.assertEqual(inj.get_owned("owned-1")["state"], "failed")
        self.assertEqual(len(fake.created), 1)    # 没有第二次尝试

    def test_user_title_query_uses_unused_datum_id(self):
        """P0-5 教训：AddToDataDefinition 的 datumID 必须 SIMCONNECT_UNUSED。"""
        inj = self._injector("modern")
        fake = inj._sm
        fake.exception_code = 34
        fake.exception_once = True
        fake.user_title = "A350-900 (Default Cabin)"
        inj.spawn({"callsign": "OF001"})
        self.assertTrue(fake.definitions)
        self.assertTrue(all(d[2] == 0xFFFFFFFF for d in fake.definitions))

    def test_spawn_uses_parked_atc_when_airport_given(self):
        """P0-12/A 路径：spec.airport → AICreateParkedATC（ATC 管辖），
        内建 AI-ATC 才会在某时刻自主滑行。"""
        inj = self._injector("modern")
        owned_id = inj.spawn({"callsign": "OF001", "airport": "EGLL",
                              "model_title": "A350-900 (Default Cabin)"})
        self.assertEqual(owned_id, "owned-1")
        self.assertEqual(len(inj._sm.parked_created), 1)
        call = inj._sm.parked_created[0]
        self.assertEqual(call["airport"], "EGLL")
        self.assertEqual(call["title"], "A350-900 (Default Cabin)")
        rec = inj.get_owned("owned-1")
        self.assertEqual(rec["api"], "ParkedATC")
        self.assertEqual(rec["airport"], "EGLL")
        self.assertEqual(rec["state"], "active")

    def test_spawn_nonatc_when_no_airport(self):
        inj = self._injector("modern")
        inj.spawn({"callsign": "OF001"})
        self.assertEqual(inj._sm.parked_created, [])   # 没走 ParkedATC
        self.assertEqual(inj.get_owned("owned-1")["api"], "NonATC")

    def test_set_flight_plan_strips_extension_and_dispatches(self):
        inj = self._injector("modern")
        owned_id = inj.spawn({"callsign": "OF001", "airport": "EGLL",
                              "model_title": "A350-900 (Default Cabin)"})
        self.assertTrue(inj.set_flight_plan(owned_id, "D:\\2.pln"))
        obj_id, path = inj._sm.plans[-1]
        self.assertEqual(path, "D:\\2")          # .PLN 扩展名被剥掉
        self.assertEqual(obj_id, TEST_OBJECT_ID)  # 用 record 里的 object_id
        rec = inj.get_owned(owned_id)
        self.assertEqual(rec["flight_plan"], "D:\\2")

    def test_set_flight_plan_rejected_for_inactive(self):
        inj = self._injector("modern")
        # owned-999 不存在；owned-1 会因 failed 状态被拒
        inj._sm.exception_code = 34
        inj._sm.exception_once = True
        inj._sm.user_title = None
        inj.spawn({"callsign": "OF001"})
        self.assertFalse(inj.set_flight_plan("owned-1", "D:\\x.pln"))
        self.assertFalse(inj.set_flight_plan("owned-999", "D:\\x.pln"))

    def test_request_owned_position_updates_record(self):
        inj = self._injector("modern")
        owned_id = inj.spawn({"callsign": "OF001", "airport": "EGLL"})
        fake = inj._sm
        self.assertTrue(inj.request_owned_position(owned_id))
        # 位置轮询用新的 request id（title 查询已用完第一个）
        req = fake.data_requests[-1]
        fake.queue_position(req, 51.5, -0.46, 300.0)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if inj.get_owned(owned_id).get("position"):
                break
            time.sleep(0.05)
        rec = inj.get_owned(owned_id)
        self.assertEqual(rec["position"], (51.5, -0.46, 300.0))
        self.assertIn("position_ts", rec)

    def test_no_fallback_on_timeout(self):
        """超时不做兜底重试（避免 5s+5s 双倍等待）。"""
        inj = self._injector("modern")
        fake = inj._sm
        fake.assign_object_id = None              # 永远不回包 → 超时
        fake.user_title = "A350-900 (Default Cabin)"
        owned_id = inj.spawn({"callsign": "OF001"}, timeout=0.2)
        self.assertIsNone(owned_id)
        self.assertEqual(len(fake.created), 1)
        self.assertEqual(fake.data_requests, [])


class _FakeIndexedInjector:
    """P2 测试替身：只提供 owned_object_index()。"""

    def __init__(self, index=None):
        self._index = index or {}
        self.stopped = False

    def owned_object_index(self):
        return self._index

    def stop(self):
        self.stopped = True


class TrafficManagerOwnedMarkTests(unittest.TestCase):
    """P2：自有 AI 进同一张 aircraft 表，多一个 owned=True 标记。"""

    def _manager(self):
        from core.traffic_manager import TrafficStateManager
        cfg = {"traffic": {"enabled": True, "msfs_ai_enabled": False,
                           "owned": {"enabled": False}}}
        bridge = SimpleNamespace(connected=True, sm=None)
        return TrafficStateManager(cfg, bridge, socketio=None)

    @staticmethod
    def _ai(object_id, callsign="OF001"):
        return {
            "callsign": callsign, "latitude": 40.64, "longitude": -73.77,
            "altitude": 13, "heading": 310, "airspeed": 0,
            "vertical_speed": 0, "on_ground": True,
            "aircraft_type": "A20N", "wake_category": "M",
            "assigned_runway": "13L", "object_id": object_id,
        }

    def test_no_injector_attached_leaves_owned_false(self):
        mgr = self._manager()
        mgr._process_ai_object(self._ai(TEST_OBJECT_ID))
        ac = mgr.aircraft["OF001"]
        self.assertFalse(ac.owned)
        self.assertIsNone(ac.owned_id)

    def test_owned_object_gets_marked(self):
        mgr = self._manager()
        mgr.set_owned_traffic(_FakeIndexedInjector({TEST_OBJECT_ID: "owned-1"}))
        mgr._process_ai_object(self._ai(TEST_OBJECT_ID))
        ac = mgr.aircraft["OF001"]
        self.assertTrue(ac.owned)
        self.assertEqual(ac.owned_id, "owned-1")
        # 字段形态与 FSLTL 枚举机一致（同表无差别）
        self.assertEqual(ac.aircraft_type, "A20N")
        self.assertEqual(ac.assigned_runway, "13L")

    def test_fsltl_object_not_marked(self):
        mgr = self._manager()
        mgr.set_owned_traffic(_FakeIndexedInjector({TEST_OBJECT_ID: "owned-1"}))
        mgr._process_ai_object(self._ai(9999, callsign="CES123"))
        self.assertFalse(mgr.aircraft["CES123"].owned)

    def test_index_lookup_failure_does_not_break_scan(self):
        class _Broken:
            def owned_object_index(self):
                raise RuntimeError("injector exploded")
        mgr = self._manager()
        mgr.set_owned_traffic(_Broken())
        mgr._process_ai_object(self._ai(TEST_OBJECT_ID))
        ac = mgr.aircraft["OF001"]
        self.assertFalse(ac.owned)            # 降级：当普通 FSLTL 机处理

    def test_bulk_update_includes_owned_fields(self):
        mgr = self._manager()
        mgr.set_owned_traffic(_FakeIndexedInjector({TEST_OBJECT_ID: "owned-1"}))
        emitted = []
        mgr.socketio = SimpleNamespace(emit=lambda name, payload:
                                       emitted.append((name, payload)))
        mgr._process_ai_object(self._ai(TEST_OBJECT_ID))
        mgr._emit_bulk_update()
        _name, payload = emitted[-1]
        row = next(r for r in payload if r["callsign"] == "OF001")
        self.assertTrue(row["owned"])
        self.assertEqual(row["owned_id"], "owned-1")

    def test_update_aircraft_sets_owned_flags(self):
        mgr = self._manager()
        mgr.update_aircraft("OF001", {"latitude": 1.0, "longitude": 2.0,
                                      "owned": True, "owned_id": "owned-1"})
        ac = mgr.aircraft["OF001"]
        self.assertTrue(ac.owned)
        self.assertEqual(ac.owned_id, "owned-1")

    def test_stop_stops_attached_injector(self):
        mgr = self._manager()
        injector = _FakeIndexedInjector({TEST_OBJECT_ID: "owned-1"})
        mgr.set_owned_traffic(injector)
        mgr.stop()
        self.assertTrue(injector.stopped)


if __name__ == "__main__":
    unittest.main(verbosity=2)
