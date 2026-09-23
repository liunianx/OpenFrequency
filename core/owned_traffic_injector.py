"""
owned_traffic_injector.py — 自有 AI 飞机注入器（混合自有交通计划 P1，
见 docs/hybrid-owned-traffic-plan.md §4 P1）。

定位：FSLTL 交通保持只读不变；本模块自建 1~N 架**自有 AI 飞机**——因为
SimObject 由本项目自己创建，SimConnect 所有权规则允许对其下发指令
（SetDataOnSimObject / 飞行计划 / 移除），这是"指挥起飞"的唯一合法路径。

设计要点（全部对齐 core/simconnect_traffic.py 已验证的模式）：
  · **独立 SimConnect 实例 + 独立 dispatch 线程**：SimConnect 实例非线程
    安全，绝不复用读通道（simconnect_traffic）或遥测通道的实例。
  · **直接打 DLL 函数表**：python-SimConnect 0.4.8 未封装 AI 创建系列
    函数（AICreateNonATCAircraft / AIRemoveObject），与读通道同一方案。
  · **自有 dispatch proc**：读通道靠子类覆写 handle_simobject_event 收
    BYTYPE 回包；本模块要收的是 SIMCONNECT_RECV_ASSIGNED_OBJECT_ID /
    SIMCONNECT_RECV_EXCEPTION（0.4.8 的默认分发不处理这两类），因此用
    ctypes 回调函数自接，与 P0 spike（tests/spikes/spike_owned_ai.py）一致。
  · **失败一律降级**：任一 DLL 调用异常 / MSFS 未运行 / DLL 缺能力 →
    start() 或 spawn() 记日志并标记失败，绝不抛给调用方、绝不影响其它
    owned_id，更不影响 FSLTL 只读通道。

线程模型硬约束（从 simconnect_traffic.py 继承）：
  0.4.8 基类无后台 dispatch 线程，connect() 返回后全库再无 CallDispatch
  调用点；本模块必须自建 daemon 线程泵 CallDispatch，否则
  ASSIGNED_OBJECT_ID 回包无人接收、spawn 永远 pending 直至超时。

[需实机验证]（P0，见计划 §9）：AICreateNonATCAircraft / AIRemoveObject
的签名与行为、MSFS 由尾号推导 ATC ID 的规则（决定 P2 去重标记是否命中）。
离线环境（无 MSFS）start() 会失败——这是预期降级，不是错误。

**P0 实测教训（2026-09-22 实机，已修复）**：python-SimConnect 库的
Attributes.py 给 AICreateNonATCAircraft / AIRemoveObject 等声明了
argtypes，结构体参数必须是**库自己的** SIMCONNECT_DATA_INITPOSITION 类
实例——本地另定义同名同布局类会在调用时抛
`ArgumentError: expected SIMCONNECT_DATA_INITPOSITION instance instead of
SIMCONNECT_DATA_INITPOSITION`（同名不同类）。且 requirements 锁定的
0.4.8 把 title/tail 两个 c_char_p 误声明为 c_double。本模块在 start() 里
一次性把 argtypes 改写为 SDK 正确签名（`_repair_ai_create_argtypes`），
结构体一律用库类构造（`_library_initpos_class`），ID 一律传纯 int
（库的 CtypesEnum.from_param 是 int(obj)，3.14 上 int(DWORD实例) 会炸）。

**P0 第二轮教训（同族第三坑）**：CallDispatch 的回调包装类型同样只认库
自己的 DispatchProc（type(sm.my_dispatch_proc_rd)）——库类型签名里的
POINTER(SIMCONNECT_RECV) 指向库模块的 RECV 类，本地同款签名自造的是
另一个原型缓存类，传入即
`ArgumentError: expected WinFunctionType instance instead of
WinFunctionType`。见 `_make_dispatch_proc`。

**P0 第七轮实测结论（MSFS 2024，P0-1 已达成）**：创建点必须在用户
现实气泡内（取用户附近即可）；**title 必须真实存在**——MSFS2024 部分
装机没有 "Airbus A320 Neo Asobo" 等 Asobo 默认机容器（报
CREATE_OBJECT_FAILED 22），可用 title 取自用户当前飞机的 TITLE simvar
（如 'A350-900 (Default Cabin)'，涂装式容器名）。EXCEPTION 回包按
**发送包 id**（GetLastSentPacketID）关联请求，见 `_last_sent_packet_id`。
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time

try:  # 与 traffic_manager 一致：事件总线缺失/异常都不能拖垮注入器
    from .context import event_bus
except Exception:  # pragma: no cover - 上下文模块理论上总在
    event_bus = None

# ── SDK 常量（RECV id）。以官方 SIMCONNECT_RECV_ID 枚举为准；若 P0 实测
#    发现值不符，以实测为准并同步修订本文件与 P0 spike。──
RECV_ID_EXCEPTION = 1
RECV_ID_OPEN = 2
RECV_ID_SIMOBJECT_DATA = 8
RECV_ID_ASSIGNED_OBJECT_ID = 12

DWORD = ctypes.c_uint32

# datumID 占位（库 Constants.SIMCONNECT_UNUSED = DWORD_MAX）。P0 probe 实测：
# AddToDataDefinition 最后一参传 0 会把多个 datum 挂到 client data 0 上，
# 每个 DUPLICATE_ID，整个定义报废。
SIMCONNECT_UNUSED = 0xFFFFFFFF

# SIMCONNECT_DATATYPE 回退值（库 Enum 不可用时用，与
# simconnect_traffic._DATATYPE_VALUES 同源）
_DATATYPE_STRING256 = 9
_DATATYPE_FLOAT64 = 4


def _datatype_string256():
    """STRING256 的 SIMCONNECT_DATATYPE 值；库不可用（开发/测试机）时
    用本地常量。"""
    try:
        from SimConnect.Enum import SIMCONNECT_DATATYPE
        return SIMCONNECT_DATATYPE.SIMCONNECT_DATATYPE_STRING256
    except Exception:
        return _DATATYPE_STRING256


def _datatype_float64():
    """FLOAT64 的 SIMCONNECT_DATATYPE 值；库不可用时用本地常量。"""
    try:
        from SimConnect.Enum import SIMCONNECT_DATATYPE
        return SIMCONNECT_DATATYPE.SIMCONNECT_DATATYPE_FLOAT64
    except Exception:
        return _DATATYPE_FLOAT64# ── ctypes 结构（按 MSFS SDK 定义；[需实机验证] 字段顺序/对齐，P0 核对）。
#    与 P0 spike 中的定义保持一致——两处任一处改动必须同步另一处。──


class SIMCONNECT_DATA_INITPOSITION(ctypes.Structure):
    _fields_ = [
        ("Latitude", ctypes.c_double),
        ("Longitude", ctypes.c_double),
        ("Altitude", ctypes.c_double),   # feet
        ("Pitch", ctypes.c_double),
        ("Bank", ctypes.c_double),
        ("Heading", ctypes.c_double),
        ("OnGround", DWORD),             # 1 = on ground
        ("Airspeed", DWORD),             # knots；0 = 停住
    ]


class SIMCONNECT_RECV(ctypes.Structure):
    _fields_ = [("dwSize", DWORD), ("dwVersion", DWORD), ("dwID", DWORD)]


class SIMCONNECT_RECV_ASSIGNED_OBJECT_ID(ctypes.Structure):
    _fields_ = [
        ("dwSize", DWORD), ("dwVersion", DWORD), ("dwID", DWORD),
        ("dwRequestID", DWORD), ("dwObjectID", DWORD),
    ]


class SIMCONNECT_RECV_EXCEPTION(ctypes.Structure):
    # 字段序列对齐 python-SimConnect Enum.py（MSFS SDK）：头 3 DWORD 后是
    # 5 个 DWORD。注意：实测偏移 16 处的 UNKNOWN_SENDID 装的其实是
    # **发送包 id**（SimConnect_GetLastSentPacketID 的返回值，库
    # RequestList.py:106/SimConnect.py:67 正是用它关联异常的），而我们
    # 的 request id 在 dwSendID 位（新版库该位语义曾变化）。关联时两个都试。
    _fields_ = [
        ("dwSize", DWORD), ("dwVersion", DWORD), ("dwID", DWORD),
        ("dwException", DWORD),
        ("UNKNOWN_SENDID", DWORD),
        ("dwSendID", DWORD),
        ("UNKNOWN_INDEX", DWORD),
        ("dwIndex", DWORD),
    ]


class SIMCONNECT_RECV_SIMOBJECT_DATA(ctypes.Structure):
    # 布局对齐库 Enum.py：dwData 前有 dwentrynumber/dwoutof/dwDefineCount
    # 三个 DWORD（漏定义 → dwData 偏移 12 字节，float64 全解错，P0-5 实测）。
    _fields_ = [
        ("dwSize", DWORD), ("dwVersion", DWORD), ("dwID", DWORD),
        ("dwRequestID", DWORD), ("dwObjectID", DWORD),
        ("dwDefineID", DWORD), ("dwFlags", DWORD),
        ("dwentrynumber", DWORD), ("dwoutof", DWORD), ("dwDefineCount", DWORD),
        ("dwData", DWORD * 8192),
    ]


# void CALLBACK DispatchProc(SIMCONNECT_RECV* pData, DWORD cbData, void* pContext)
if sys.platform == "win32":
    DispatchProcType = ctypes.WINFUNCTYPE
else:  # 仅为在非 Windows 上能 import / 跑测试（不会真连 sim）
    DispatchProcType = ctypes.CFUNCTYPE

DispatchProc = DispatchProcType(
    None, ctypes.POINTER(SIMCONNECT_RECV), DWORD, ctypes.c_void_p)

# ── 自有 AI 状态机（P1 只需要创建/清理；滑行起飞驱动是 P3 controller 的事）──
STATE_PENDING = "pending"      # 已调 AICreateNonATCAircraft，等 ASSIGNED_OBJECT_ID
STATE_ACTIVE = "active"        # 拿到 dwObjectID，存活中
STATE_FAILED = "failed"        # DLL 异常 / 超时 / 收到 EXCEPTION 回包

_DISPATCH_POLL_SECONDS = 0.02  # 与读通道一致的 dispatch 泵占空比
DEFAULT_MAX_AIRCRAFT = 2       # 计划 §5：默认最多自建 2 架（控制成本/性能）
DEFAULT_SPAWN_TIMEOUT = 5.0    # 等 ASSIGNED_OBJECT_ID 回包的上限（秒）
_REQUEST_ID_BASE = 0x9000      # 本实例私有 request id 起点（独立实例，无碰撞面）
_DEDUP_SUFFIX_LIMIT = 99       # 呼号去重最多尝试 base-2 … base-99


def _ascii(text) -> bytes:
    """SimConnect 系列 API 走窄字符串；非法字符静默剔除，避免 UnicodeEncodeError
    把 spawn 变成异常（如尾号里混进中文用户输入）。"""
    if text is None:
        return b""
    return str(text).encode("ascii", "ignore")


def _library_initpos_class():
    """取 python-SimConnect 库自己的 SIMCONNECT_DATA_INITPOSITION 类。

    P0 实测根因：库的 Attributes.py 给 AI 创建函数族声明了 argtypes，
    结构体参数必须是库那个**类对象**的实例——本地再定义一个字段完全一致
    的类，ctypes 也只会回一句"expected SIMCONNECT_DATA_INITPOSITION
    instance instead of SIMCONNECT_DATA_INITPOSITION"（同名不同类）。
    库没装（开发/测试机）时返回 None，调用方退回本地定义（布局一致，
    只是没有真库的 argtypes 校验）。带缓存，避免每架 spawn 都 import。
    """
    try:
        from SimConnect.Enum import SIMCONNECT_DATA_INITPOSITION as cls
        return cls
    except Exception:
        return None


class OwnedTrafficInjector:
    """自有 AI 飞机生命周期管理：创建（spawn）/ 移除（despawn）。

    用法：
        injector = OwnedTrafficInjector(config, dedup_provider=reader_callsigns)
        injector.start()                  # 失败 → available=False，调用方降级
        owned_id = injector.spawn(spec)   # None=失败/达上限（已记日志）
        ...
        injector.despawn(owned_id)        # 或 despawn_all()
        injector.stop()                   # 进程退出 / 场景切换 / 开关关闭

    spec（全部可选，按机场现场由调用方填）：
        model_title  str  已装机模型容器名（计划 §6：只能用用户已装机模；
                          空=默认 AI 机，外观"素"但功能完整）
        callsign     str  自有 AI 呼号（去重对象；默认回退尾号）
        tail_number  str  尾号（默认=callsign）
        latitude / longitude / altitude_ft / heading / on_ground / airspeed_kt

    [需实机验证] MSFS 由尾号推导 ATC ID 的规则——若推导出的 ATC ID 与
    callsign 不一致，P2 按 callsign 去重时会漏标记；P0 spike (`spawn` 模式)
    要在真机上核对这一点，结论记入计划 §9。
    """

    def __init__(self, config=None, connect_fn=None, dedup_provider=None,
                 initpos_class=None):
        owned_cfg = ((config or {}).get("traffic", {}) or {}).get("owned", {}) or {}
        # 总开关默认 false：关闭时本实例完全不连 SimConnect，
        # 行为等同升级前（纯 FSLTL 只读）。三个回退开关互不牵连（计划 §5）。
        self.enabled = bool(owned_cfg.get("enabled", False))
        try:
            self.max_aircraft = max(0, int(owned_cfg.get(
                "max_aircraft", DEFAULT_MAX_AIRCRAFT)))
        except (TypeError, ValueError):
            self.max_aircraft = DEFAULT_MAX_AIRCRAFT
        self._connect_fn = connect_fn          # 测试注入：假 SimConnect 工厂
        self._dedup_provider = dedup_provider  # 测试注入：返回已占用呼号的 callable

        self._sm = None
        self._dll = None
        self._thread = None
        self._stop_event = threading.Event()
        self._available = False
        self._last_error = None

        self._lock = threading.RLock()
        self._owned = {}          # owned_id -> record dict
        self._object_index = {}   # dwObjectID -> owned_id（P2 回灌用）
        self._pending = {}        # request_id -> owned_id（等回包；含"僵尸"登记）
        self._pending_by_packet = {}  # 发送包 id -> owned_id（P0 probe 实测：
                                       # EXCEPTION 按发送包 id 关联，非 request id）
        self._spawn_events = {}   # owned_id -> threading.Event（spawn 同步等待）
        self._zombie_requests = {}  # request_id -> owned_id：pending 中被 despawn，
                                    # 回包晚到时必须立刻 AIRemoveObject，否则留孤儿
        self._pending_data = {}   # request_id -> Event（用户上下文数据请求）
        self._pos_requests = {}   # request_id -> owned_id（位置轮询回包路由）
        self._user_title = None   # 用户当前飞机容器 title（TITLE simvar，缓存）
        self._user_title_checked = False
        self._request_seq = _REQUEST_ID_BASE
        self._owned_seq = 0
        # P0 教训：InitPos 结构体必须用库自己的类构造（argtypes 身份校验）。
        # initpos_class 仅供测试注入替身类；生产走 _library_initpos_class()。
        self._initpos_cls = (initpos_class or _library_initpos_class()
                             or SIMCONNECT_DATA_INITPOSITION)
        # GC 护栏：ctypes 回调被 native 侧持有，Python 侧必须留强引用，
        # 否则回调可能被回收后 CallDispatch 调用野指针。
        self._dispatch_proc = None

    # ── 生命周期 ────────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return self._available

    @property
    def last_error(self):
        return self._last_error

    def start(self) -> bool:
        """建立独立 SimConnect 连接并启动 dispatch 线程。失败返回 False（不抛）。"""
        if self._available:
            return True
        if not self.enabled:
            self._last_error = "traffic.owned.enabled=false"
            print("OwnedTrafficInjector: disabled in config (traffic.owned.enabled)")
            return False
        try:
            sm = self._connect_simconnect()
        except Exception as e:
            self._last_error = str(e)
            print(f"OwnedTrafficInjector: unavailable — {e}")
            return False

        dll = getattr(sm, "dll", None)
        if dll is None or not hasattr(dll, "AICreateNonATCAircraft") \
                or not hasattr(dll, "AIRemoveObject"):
            self._last_error = "SimConnect.dll 缺少 AICreateNonATCAircraft/AIRemoveObject"
            print(f"OwnedTrafficInjector: unavailable — {self._last_error}")
            self._safe_exit()
            return False

        self._sm = sm
        self._dll = dll
        self._repair_ai_create_argtypes()
        self._dispatch_proc = self._make_dispatch_proc()
        self._install_dispatch_chain()
        self._start_dispatch()
        self._available = True
        print("OwnedTrafficInjector: started (independent SimConnect client, "
              f"max_aircraft={self.max_aircraft})")
        return True

    def _repair_ai_create_argtypes(self):
        """把 AICreateNonATCAircraft 的 argtypes 改写为 SDK 正确签名。

        两个原因（均来自 P0 实机实测）：
        1. requirements 锁定的 python-SimConnect 0.4.8 把 szContainerTitle /
           szTailNumber 两个 c_char_p 误声明为 c_double——不修则连 bytes
           字符串都传不进去（ArgumentError: must be real number, not bytes）；
        2. 新版库虽已修正 c_char_p，但 argtypes 仍要求**库自己的**
           SIMCONNECT_DATA_INITPOSITION 类。

        argtypes 挂在 cdll 内部缓存的函数指针对象上（全进程共享同一
        _FuncPtr）；本函数是 AI 创建族的唯一调用方，改写影响面为零。
        设置失败（替身/未来版本变了形态）不致命，记日志继续。
        """
        try:
            fn = self._dll.AICreateNonATCAircraft
            fn.argtypes = [ctypes.c_void_p,   # HANDLE
                           ctypes.c_char_p,   # szContainerTitle
                           ctypes.c_char_p,   # szTailNumber
                           self._initpos_cls,  # SIMCONNECT_DATA_INITPOSITION
                           ctypes.c_uint32]   # RequestID
        except Exception as e:
            print(f"OwnedTrafficInjector: argtypes repair skipped — {e!r}")

    def _connect_simconnect(self):
        if self._connect_fn is not None:
            # 测试注入：假实例。同样兜 SystemExit（0.4.8 connect() 在 MSFS
            # 未运行时 exit(0)，注入的替身可能模拟该行为）。
            try:
                return self._connect_fn()
            except SystemExit:
                raise ConnectionError(
                    "SimConnect exited during connect (MSFS not running?)")
        # 与读通道同一坑（simconnect_traffic._connect_simconnect）：
        # 0.4.8 connect() 在 MSFS 未运行时 exit(0) → SystemExit 穿透
        # except Exception，必须显式兜住，否则会杀死调用方线程。
        try:
            from SimConnect import SimConnect
            return SimConnect(auto_connect=True)
        except SystemExit:
            raise ConnectionError("SimConnect exited during connect (MSFS not running?)")

    def _make_dispatch_proc(self):
        """构造 ctypes dispatch 回调，把 ASSIGNED_OBJECT_ID / EXCEPTION 接进来。

        不用 python-SimConnect 自带的 my_dispatch_proc_rd 的函数体：它只
        处理库自己关心的回包类型（如 SIMOBJECT_DATA），不处理
        ASSIGNED_OBJECT_ID。

        P0 第三轮教训：回调**包装类型**也必须用库自己的
        DispatchProc（type(sm.my_dispatch_proc_rd)）。库的 CallDispatch
        argtypes = [HANDLE, 该类型, c_void_p]，而类型签名里的
        POINTER(SIMCONNECT_RECV) 指向库模块的 RECV 类——本地用同款签名
        自造的是另一个原型缓存类对象，传入即
        `ArgumentError: expected WinFunctionType instance instead of
        WinFunctionType`（第三只同名不同类的坑）。取库实例的类型零假设；
        替身环境（无 my_dispatch_proc_rd）才退回本地 DispatchProc。
        """
        def _proc(pData, cbData, pContext):
            self._on_recv(pData)

        base = getattr(self._sm, "my_dispatch_proc_rd", None)
        proc_cls = type(base) if base is not None else DispatchProc
        return proc_cls(_proc)

    def _on_recv(self, pData):
        """自建泵与库后台泵共用的回包入口（见 _install_dispatch_chain）。"""
        try:
            dwid = pData.contents.dwID
            if dwid == RECV_ID_ASSIGNED_OBJECT_ID:
                rec = ctypes.cast(
                    pData, ctypes.POINTER(
                        SIMCONNECT_RECV_ASSIGNED_OBJECT_ID)).contents
                self._on_assigned_object_id(rec.dwRequestID, rec.dwObjectID)
            elif dwid == RECV_ID_EXCEPTION:
                rec = ctypes.cast(
                    pData, ctypes.POINTER(
                        SIMCONNECT_RECV_EXCEPTION)).contents
                # P0 probe 实测：异常回包按"发送包 id"关联请求
                # （库用 GetLastSentPacketID 的返回值做同样的事）；
                # 该值在 UNKNOWN_SENDID 位，dwSendID 位语义随版本变化，都试。
                send_id = rec.UNKNOWN_SENDID or rec.dwSendID
                self._on_exception(send_id, rec.dwException)
            elif dwid == RECV_ID_SIMOBJECT_DATA:
                rec = ctypes.cast(
                    pData, ctypes.POINTER(
                        SIMCONNECT_RECV_SIMOBJECT_DATA)).contents
                self._on_user_data(rec.dwRequestID, rec.dwData)
        except Exception as e:  # noqa: BLE001 — 只记录，不中断泵
            self._last_error = str(e)

    def _on_user_data(self, request_id, dw_data):
        """SIMOBJECT_DATA 回包路由：位置轮询（3×float64）优先，
        其余（TITLE string256）走用户上下文通道。"""
        with self._lock:
            owned_id = self._pos_requests.pop(request_id, None)
        if owned_id is not None:
            try:
                raw = ctypes.string_at(ctypes.byref(dw_data), 24)
                vals = ctypes.cast(raw, ctypes.POINTER(
                    ctypes.c_double * 3)).contents
                with self._lock:
                    rec = self._owned.get(owned_id)
                    if rec is not None:
                        rec["position"] = tuple(vals)
                        rec["position_ts"] = time.time()
            except Exception as e:  # noqa: BLE001
                self._last_error = f"position parse failed: {e!r}"
            return
        with self._lock:
            event = self._pending_data.pop(request_id, None)
        if event is None:
            return
        try:
            raw = ctypes.string_at(ctypes.byref(dw_data), 256)
            self._user_title = raw.split(b"\x00", 1)[0].decode("ascii", "ignore") \
                or None
        except Exception:  # noqa: BLE001
            self._user_title = None
        event.set()

    def _install_dispatch_chain(self):
        """让我们的回包处理同时覆盖两条投递路径。

        P0 probe 实测（0.4.26）：新版 python-SimConnect 的 connect() 会起
        后台 timerThread 持续 CallDispatch 泵 my_dispatch_proc_rd，与我们
        自建的泵**竞争同一个消息队列**——ASSIGNED_OBJECT_ID 可能被库线程
        赢走（库只把它塞进环境变量 SIMCONNECT_OBJECT_ID，我们这边看不到），
        spawn 就会一直等到超时。0.4.8 无后台线程，只有自建泵，包装无副作用。

        方案：包一层库的 my_dispatch_proc（我们的 _on_recv → 库原函数），
        并用库自己的类型重建 my_dispatch_proc_rd——此后不论哪条泵赢走
        回包，都先过我们的处理器，再走库的默认处理（不破坏库行为）。
        """
        sm = self._sm
        original = getattr(sm, "my_dispatch_proc", None)
        if original is None:
            return
        injector = self

        def _chained(pData, cbData, pContext):
            try:
                injector._on_recv(pData)
            except Exception:  # noqa: BLE001 — 链上异常不拖垮库处理
                pass
            return original(pData, cbData, pContext)

        sm.my_dispatch_proc = _chained
        proc_type = type(getattr(sm, "my_dispatch_proc_rd", None))
        if proc_type is not None:
            try:
                # 已建的回调包装的是旧函数对象，必须重建才指向 _chained
                sm.my_dispatch_proc_rd = proc_type(_chained)
            except Exception as e:  # noqa: BLE001 — 重建失败退回自建泵
                print(f"OwnedTrafficInjector: dispatch chain rebuild "
                      f"failed — {e!r}")

    def _start_dispatch(self):
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._dispatch_loop, name="OwnedTrafficDispatch", daemon=True)
        self._thread.start()

    def _stop_dispatch(self):
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
        self._thread = None

    def _dispatch_loop(self):
        sm = self._sm
        dll = self._dll
        if sm is None or dll is None or not hasattr(dll, "CallDispatch"):
            return                    # 不完整客户端（如测试替身）：无可泵
        while not self._stop_event.is_set():
            try:
                dll.CallDispatch(sm.hSimConnect, self._dispatch_proc, None)
            except OSError:
                # 句柄已关：正常 stop 竞态，或 sim 退出/断连（计划 §8）。
                if not self._stop_event.is_set():
                    self._on_connection_lost()
                break
            except Exception as e:
                self._last_error = str(e)
            time.sleep(_DISPATCH_POLL_SECONDS)

    def _on_connection_lost(self):
        """SimConnect 断链（sim 退出 / 崩溃）：标记不可用并清本地登记。

        计划 §8 风险表：断连后句柄失效，继续 spawn 只会全量异常；
        owned 记录留着会变"幽灵"（controller tick / policy 误判）。
        SimObject 本身随 sim 退出而消失，无需（也无法）AIRemoveObject。
        重连需外部重新 start()（app 重启或显式重启注入器）。
        """
        if not self._available:
            return
        self._available = False
        self._last_error = "SimConnect connection lost"
        print("OwnedTrafficInjector: connection lost — clearing owned records")
        with self._lock:
            self._object_index.clear()
            self._pos_requests.clear()
            owned_ids = list(self._owned)
        for owned_id in owned_ids:
            self._fail(owned_id, "SimConnect connection lost")
        self._emit("owned_traffic_unavailable", {"reason": "connection_lost"})

    def _safe_exit(self):
        sm, self._sm = self._sm, None
        self._dll = None
        if sm is not None:
            try:
                sm.exit()
            except Exception:
                pass

    def stop(self):
        """进程退出 / 场景切换 / 开关关闭：先清飞机、再停线程、后关句柄
        （顺序与读通道一致，避免对已 Close 的 handle 调 CallDispatch）。

        注意顺序：despawn_all 必须发生在 _available 置 False 之前——
        despawn 内部按 _available 决定是否真的调 AIRemoveObject。
        """
        try:
            self.despawn_all()
        except Exception as e:
            print(f"OwnedTrafficInjector: despawn_all error — {e}")
        self._available = False
        self._stop_dispatch()
        self._safe_exit()

    # ── 创建 / 移除 ────────────────────────────────────────────────────────

    def spawn(self, spec, timeout=DEFAULT_SPAWN_TIMEOUT):
        """创建一架自有 AI，返回 owned_id；失败/达上限返回 None（已记日志）。

        同步等待 ASSIGNED_OBJECT_ID 回包（低频操作，阻塞可接受）；
        超时或收到 EXCEPTION 回包 → 标记该 owned_id failed 并返回 None。

        **title 兜底**（P0-7/8 MSFS2024 实测）：MSFS2024 上"空 title=默认
        AI 机"与多数 Asobo 默认机容器名不存在（CREATE_OBJECT_FAILED 22 /
        OBJECT_CONTAINER 34），本机唯一可用容器常常就是用户正在飞的飞机。
        创建被拒时自动用用户当前飞机 title 重试一次。

        **创建 API 选择**（P0-12 实测，A 路径结论）：`spec["airport"]` 带
        ICAO 时走 `AICreateParkedATCAircraft`（停机位创建、ATC 管辖）——
        内建 AI-ATC 才会在约 90-120s 排班延迟后自主推出滑行（配合
        set_flight_plan 放行）。不带 airport 走 `AICreateNonATCAircraft`
        （按坐标创建、不受 ATC 管辖，用于 P3-B 自驱动的静态目标）。
        """
        if not self._available or self._sm is None:
            self._last_error = "injector not available"
            print(f"OwnedTrafficInjector: spawn rejected — {self._last_error}")
            return None

        owned_id = self._spawn_once(spec, timeout)
        if owned_id is not None:
            return owned_id

        # 兜底：被判"创建失败/容器不存在"类拒绝 → 换用户当前飞机 title 再试一次
        spec = dict(spec or {})
        reason = self._last_error or ""
        if "exception 22" in reason or "exception 34" in reason:
            fallback_title = self._user_aircraft_title()
            current = spec.get("model_title") or ""
            if fallback_title and fallback_title != current:
                print(f"OwnedTrafficInjector: spawn rejected ({reason.strip()}); "
                      f"retrying with user aircraft title "
                      f"{fallback_title!r}")
                spec["model_title"] = fallback_title
                owned_id = self._spawn_once(spec, timeout)
                if owned_id is not None:
                    return owned_id
        return None

    def _spawn_once(self, spec, timeout):
        """单次创建尝试（spawn 的内部实现；title 兜底由 spawn 负责）。"""
        spec = dict(spec or {})
        with self._lock:
            alive = sum(1 for rec in self._owned.values()
                        if rec["state"] != STATE_FAILED)
            if alive >= self.max_aircraft:
                print(f"OwnedTrafficInjector: max_aircraft={self.max_aircraft} "
                      f"reached, spawn refused")
                return None
        callsign = self._dedup_callsign(
            spec.get("callsign") or spec.get("tail_number") or "OF000")

        with self._lock:
            self._owned_seq += 1
            self._request_seq += 1
            owned_id = f"owned-{self._owned_seq}"
            request_id = self._request_seq
            event = threading.Event()
            self._owned[owned_id] = {
                "owned_id": owned_id,
                "object_id": None,
                "callsign": callsign,
                "state": STATE_PENDING,
                "spec": spec,
                "request_id": request_id,
                "spawned_at": time.time(),
                "error": None,
            }
            self._pending[request_id] = owned_id
            self._spawn_events[owned_id] = event

        api = "ParkedATC" if spec.get("airport") else "NonATC"
        try:
            # P0 教训：InitPos 用库类构造（start() 已修 argtypes），ID 传纯
            # int（库的 CtypesEnum.from_param=int(obj)，某些 Python 版本上
            # int(DWORD 实例) 会抛 ValueError，纯 int 全版本安全）。
            airport = spec.get("airport")
            if airport:
                # A 路径（P0-12）：停机位创建、ATC 管辖 → 飞行计划生效后
                # 内建 AI-ATC 自主推出滑行。库该函数 argtypes 本就正确
                # （bytes/int 直通），无需修。
                hr = self._dll.AICreateParkedATCAircraft(
                    self._sm.hSimConnect,
                    _ascii(spec.get("model_title")),
                    _ascii(spec.get("tail_number") or callsign),
                    _ascii(airport),
                    request_id)
                api = "ParkedATC"
            else:
                hr = self._dll.AICreateNonATCAircraft(
                    self._sm.hSimConnect,
                    _ascii(spec.get("model_title")),
                    _ascii(spec.get("tail_number") or callsign),
                    self._init_position(spec),
                    request_id)
                api = "NonATC"
            print(f"OwnedTrafficInjector: AICreate{api} "
                  f"{callsign} req={request_id} → HRESULT={hr}")
            with self._lock:
                self._owned[owned_id]["api"] = api
                self._owned[owned_id]["airport"] = airport
            # P0 probe 实测：EXCEPTION 回包按"发送包 id"关联请求（不是
            # request id）。库用 GetLastSentPacketID 做同样的事；拿不到
            # （旧版无此函数/替身）则退回 request id 键匹配。
            packet_id = self._last_sent_packet_id()
            if packet_id is not None:
                with self._lock:
                    self._owned[owned_id]["packet_id"] = packet_id
                    self._pending_by_packet[packet_id] = owned_id
        except Exception as e:  # noqa: BLE001 — 单架失败降级，不影响其它
            self._fail(owned_id, f"AICreate{api} raised {e!r}")
            return None

        if not event.wait(timeout):
            self._fail(owned_id, f"timed out waiting for ASSIGNED_OBJECT_ID "
                                 f"({timeout}s)")
            return None
        with self._lock:
            rec = self._owned.get(owned_id)
        if rec is None or rec["state"] != STATE_ACTIVE:
            # _fail 已记录原因
            return None
        return owned_id

    def set_flight_plan(self, owned_id, pln_path):
        """赋予飞行计划（A 路径的"放行"动作，P0-12 实测成立）。

        sequencer 放行 → controller 调用本方法 → 内建 AI-ATC 在约
        90-120s 排班延迟后自主推出/滑行/起飞。**不带计划 = 永停**
        （P0-1 已证），因此"赋予计划的时机"就是放行门控。

        pln_path 允许带或不带 .PLN 扩展名（SDK 要求不带）。
        返回 True=调用被接受（S_OK）。EXCEPTION 异步到达，写进 record。
        """
        with self._lock:
            rec = self._owned.get(owned_id)
        if rec is None or rec.get("state") != STATE_ACTIVE:
            print(f"OwnedTrafficInjector: set_flight_plan rejected — "
                  f"{owned_id} not active")
            return False
        if not hasattr(self._dll, "AISetAircraftFlightPlan"):
            print("OwnedTrafficInjector: DLL 无 AISetAircraftFlightPlan")
            return False
        path = pln_path[:-4] if pln_path.lower().endswith(".pln") else pln_path
        try:
            hr = self._dll.AISetAircraftFlightPlan(
                self._sm.hSimConnect, int(rec["object_id"]),
                _ascii(path), int(rec["request_id"]))
            print(f"OwnedTrafficInjector: AISetAircraftFlightPlan "
                  f"{owned_id} obj={rec['object_id']} path={path!r} → "
                  f"HRESULT={hr}")
            with self._lock:
                rec["flight_plan"] = path
                rec["plan_hr"] = hr
            return hr == 0
        except Exception as e:  # noqa: BLE001 — 单架失败只记录
            print(f"OwnedTrafficInjector: AISetAircraftFlightPlan "
                  f"{owned_id} raised {e!r}")
            return False

    def request_owned_position(self, owned_id) -> bool:
        """请求某架自有 AI 的 LAT/LON/ALT（PERIOD_ONCE）。

        回包经 _on_recv → record["position"]=(lat,lon,alt) 与
        record["position_ts"]。controller 的 1Hz tick 用它算离场距离、
        按 despawn_on_airborne_nm 回收。失败返回 False（不抛）。
        """
        with self._lock:
            rec = self._owned.get(owned_id)
        if rec is None or rec.get("state") != STATE_ACTIVE \
                or rec.get("object_id") is None:
            return False
        try:
            def_id = self._sm.new_def_id()
            with self._lock:
                self._request_seq += 1
                req_id = self._request_seq
                self._pos_requests[req_id] = owned_id
            for name in ("PLANE LATITUDE", "PLANE LONGITUDE", "PLANE ALTITUDE"):
                self._sm.dll.AddToDataDefinition(
                    self._sm.hSimConnect, def_id.value, name.encode("ascii"),
                    b"feet" if name.endswith("ALTITUDE") else b"degrees",
                    _datatype_float64(), 0, SIMCONNECT_UNUSED)
            self._sm.dll.RequestDataOnSimObject(
                self._sm.hSimConnect, req_id, def_id.value,
                int(rec["object_id"]),
                1,             # SIMCONNECT_PERIOD_ONCE
                0, 0, 0, 0)
            return True
        except Exception as e:  # noqa: BLE001 — 位置轮询失败不抛
            print(f"OwnedTrafficInjector: position request {owned_id} "
                  f"failed — {e!r}")
            return False

    def despawn(self, owned_id):
        """移除一架自有 AI（AIRemoveObject + 本地登记处摘除）。幂等。

        若该架还处于 PENDING（回包未到），登记为"僵尸请求"：MSFS 侧创建
        仍会完成，回包晚到时立刻补一次 AIRemoveObject，避免孤儿 SimObject
        （计划 §8 风险表）。
        """
        with self._lock:
            rec = self._owned.pop(owned_id, None)
            event = self._spawn_events.pop(owned_id, None)
            if rec is not None and rec.get("object_id") is not None:
                self._object_index.pop(rec["object_id"], None)
                self._pending.pop(rec.get("request_id"), None)
                if rec.get("packet_id") is not None:
                    self._pending_by_packet.pop(rec["packet_id"], None)
            elif rec is not None and rec.get("state") == STATE_PENDING:
                self._zombie_requests[rec["request_id"]] = owned_id
                if rec.get("packet_id") is not None:
                    self._pending_by_packet.pop(rec["packet_id"], None)
            # 清掉该架的位置轮询在途请求（防泄漏）
            self._pos_requests = {k: v for k, v in self._pos_requests.items()
                                  if v != owned_id}
        if rec is None:
            return False
        # 唤醒可能仍阻塞在 spawn() 里的等待方（它会看到记录已消失→返回 None）
        if event is not None:
            event.set()
        object_id = rec.get("object_id")
        if object_id is not None and self._available:
            self._remove_object(int(object_id), rec.get("request_id"))
        self._emit("owned_traffic_removed", {"owned_id": owned_id,
                                             "callsign": rec.get("callsign")})
        return True

    def _remove_object(self, object_id, request_id):
        """调 AIRemoveObject（失败只记录；本地登记已先摘除）。

        ID 传纯 int：库 argtypes 为 [HANDLE, DWORD, CtypesEnum]，int 全兼容。
        """
        if self._sm is None or self._dll is None or object_id is None:
            return
        try:
            hr = self._dll.AIRemoveObject(
                self._sm.hSimConnect, int(object_id),
                int(request_id if request_id is not None else 0))
            print(f"OwnedTrafficInjector: AIRemoveObject obj={object_id} → HRESULT={hr}")
        except Exception as e:  # noqa: BLE001 — 移除失败只记录；本地已摘除
            print(f"OwnedTrafficInjector: AIRemoveObject obj={object_id} raised {e!r}")

    def despawn_all(self):
        """移除全部自有 AI（进程退出 / 场景切换 / 开关关闭时全清，
        避免孤儿 SimObject——计划 §8 风险表）。"""
        with self._lock:
            owned_ids = list(self._owned)
        for owned_id in owned_ids:
            self.despawn(owned_id)

    # ── 状态查询（P2 回灌 / UI 标注用）─────────────────────────────────────

    def list_owned(self):
        """当前自有 AI 快照（脱密后的 dict 列表）。"""
        with self._lock:
            records = [dict(rec) for rec in self._owned.values()]
        out = []
        for rec in records:
            item = {k: v for k, v in rec.items() if k != "spec"}
            item["owned"] = True
            out.append(item)
        return out

    def owned_object_index(self):
        """{dwObjectID: owned_id}——traffic_manager 靠它把枚举到的 SimObject
        标注为自有（P2：同一张 aircraft 表，多一个 owned=True 标记）。"""
        with self._lock:
            return dict(self._object_index)

    def get_owned(self, owned_id):
        with self._lock:
            rec = self._owned.get(owned_id)
            return dict(rec) if rec else None

    # ── 回包处理（dispatch 线程上下文）─────────────────────────────────────

    def _on_assigned_object_id(self, request_id, object_id):
        with self._lock:
            owned_id = self._pending.pop(request_id, None)
            was_zombie = self._zombie_requests.pop(request_id, None)
            if owned_id is None:
                return                     # 非本模块发起的创建：忽略
            rec = self._owned.get(owned_id)
            if rec is None:
                # 已被 despawn（pending 期间）：补刀移除，防孤儿 SimObject
                zombie = True
            else:
                zombie = False
                rec["object_id"] = int(object_id)
                rec["state"] = STATE_ACTIVE
                self._object_index[int(object_id)] = owned_id
                if rec.get("packet_id") is not None:
                    self._pending_by_packet.pop(rec["packet_id"], None)
                event = self._spawn_events.get(owned_id)
        if zombie:
            print(f"OwnedTrafficInjector: late ASSIGNED_OBJECT_ID for despawned "
                  f"{owned_id} obj={object_id} — removing immediately")
            self._remove_object(int(object_id), request_id)
            return
        print(f"OwnedTrafficInjector: ASSIGNED_OBJECT_ID {owned_id} "
              f"obj={object_id} ({rec['callsign']})")
        if event is not None:
            event.set()
        self._emit("owned_traffic_spawned", {
            "owned_id": owned_id, "object_id": int(object_id),
            "callsign": rec["callsign"]})

    def _on_exception(self, send_id, exception_code):
        """EXCEPTION 回包：按发送包 id（正常路径）或 request id（退回）找
        在途 spawn 并标记 failed。都不匹配 → 只进日志（可能是别的请求的）。"""
        with self._lock:
            owned_id = self._pending_by_packet.pop(send_id, None)
            if owned_id is None:
                owned_id = self._pending.pop(send_id, None)
        if owned_id is None:
            return                         # 异常与在途 spawn 无关：只进日志
        print(f"OwnedTrafficInjector: EXCEPTION code={exception_code} "
              f"sendID={send_id} ({owned_id})")
        self._fail(owned_id, f"SimConnect exception {exception_code} "
                             f"(sendID={send_id})")

    def _last_sent_packet_id(self):
        """SimConnect_GetLastSentPacketID——刚发出的 DLL 调用的发送包 id。

        库（RequestList.py/SimConnect.py）同样用它把 EXCEPTION 关联回请求。
        旧版库无此函数或替身环境返回 None，调用方退回 request id 匹配。
        """
        getter = getattr(self._dll, "GetLastSentPacketID", None)
        if getter is None:
            return None
        try:
            out = ctypes.c_uint32(0)
            getter(self._sm.hSimConnect, ctypes.byref(out))
            return out.value or None
        except Exception:
            return None

    def _user_aircraft_title(self, timeout=2.0):
        """用户当前飞机的容器 title（TITLE simvar），会话内缓存一次。

        P0-7/8 MSFS2024 实测：该装机上没有 "Airbus A320 Neo Asobo" 等
        Asobo 默认机容器（空 title 也报 CREATE_OBJECT_FAILED 22），唯一
        可用容器是用户正在飞的飞机。spawn 用它兜底（见 spawn）。

        实现：注册一个只含 TITLE(string256) 的 data definition +
        RequestDataOnSimObject(PERIOD_ONCE) 到用户机，等 SIMOBJECT_DATA
        回包解析。任何一步失败返回 None（不抛——兜底失败就照旧失败）。
        """
        with self._lock:
            if self._user_title_checked:
                return self._user_title
            self._user_title_checked = True
        if not self._available or self._sm is None:
            return None
        try:
            def_id = self._sm.new_def_id()
            with self._lock:
                self._request_seq += 1
                req_id = self._request_seq
                event = threading.Event()
                self._pending_data[req_id] = event
            self._sm.dll.AddToDataDefinition(
                self._sm.hSimConnect, def_id.value, b"TITLE", b"",
                _datatype_string256(),
                0, SIMCONNECT_UNUSED)   # datumID 必须是 UNUSED（P0-5 教训）
            self._sm.dll.RequestDataOnSimObject(
                self._sm.hSimConnect, req_id, def_id.value,
                0,                 # SIMCONNECT_OBJECT_ID_USER
                1,                 # SIMCONNECT_PERIOD_ONCE
                0, 0, 0, 0)
            event.wait(timeout)
        except Exception as e:  # noqa: BLE001 — 兜底查询失败不抛
            print(f"OwnedTrafficInjector: user title query failed — {e!r}")
        with self._lock:
            return self._user_title

    # ── 内部工具 ───────────────────────────────────────────────────────────

    def _fail(self, owned_id, reason):
        """标记某架 spawn 失败：记日志、摘 pending、唤醒等待方。
        失败一律降级——不影响其它 owned_id，更不影响 FSLTL 只读通道。"""
        with self._lock:
            rec = self._owned.get(owned_id)
            if rec is not None:
                rec["state"] = STATE_FAILED
                rec["error"] = reason
                self._pending.pop(rec.get("request_id"), None)
                if rec.get("packet_id") is not None:
                    self._pending_by_packet.pop(rec["packet_id"], None)
            event = self._spawn_events.pop(owned_id, None)
        self._last_error = reason
        print(f"OwnedTrafficInjector: {owned_id} failed — {reason}")
        if event is not None:
            event.set()
        self._emit("owned_traffic_failed", {"owned_id": owned_id, "reason": reason})

    def _dedup_callsign(self, base):
        """避开 FSLTL 已占用呼号（计划 §1 硬约束：避免视觉重影）。

        taken 由 dedup_provider 提供（生产里接 traffic_manager 最新枚举）；
        provider 不可用/抛错时当作空集——去重是尽力而为，不阻塞 spawn。
        """
        base = str(base or "OF000").strip().upper()
        try:
            taken = {str(c).strip().upper()
                     for c in (self._dedup_provider() or []) if str(c).strip()}
        except Exception as e:  # noqa: BLE001 — 去重源故障不阻塞 spawn
            print(f"OwnedTrafficInjector: dedup provider error — {e!r}")
            taken = set()
        if base not in taken:
            return base
        for suffix in range(2, _DEDUP_SUFFIX_LIMIT + 1):
            candidate = f"{base}-{suffix}"
            if candidate not in taken:
                print(f"OwnedTrafficInjector: callsign {base} taken by existing "
                      f"traffic, using {candidate}")
                return candidate
        fallback = f"{base}-{int(time.time()) % 10000}"
        print(f"OwnedTrafficInjector: callsign {base} saturated, using {fallback}")
        return fallback

    def _init_position(self, spec):
        """spec → SIMCONNECT_DATA_INITPOSITION（用 start() 解析出的类构造，
        见 _library_initpos_class 的 P0 教训）。缺省值给"跑道口停住"的
        安全形态；调用方应按现场（停机位/等待点）显式传 lat/lon/hdg。"""
        def _num(key, default):
            try:
                return float(spec.get(key, default))
            except (TypeError, ValueError):
                return float(default)
        return self._initpos_cls(
            Latitude=_num("latitude", 0.0),
            Longitude=_num("longitude", 0.0),
            Altitude=_num("altitude_ft", 0.0),
            Pitch=_num("pitch", 0.0),
            Bank=_num("bank", 0.0),
            Heading=_num("heading", 0.0),
            OnGround=0 if spec.get("on_ground") is False else 1,
            Airspeed=int(_num("airspeed_kt", 0)),
        )

    @staticmethod
    def _emit(name, payload):
        if event_bus is None:
            return
        try:
            event_bus.emit(name, payload)
        except Exception as e:  # noqa: BLE001 — 事件消费者故障不影响注入器
            print(f"OwnedTrafficInjector: emit {name} failed — {e!r}")
