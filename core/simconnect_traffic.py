"""
simconnect_traffic.py — MSFS SimConnect AI 交通枚举（C1，FSLTL 数据基础）。

要点（均已对照 python-SimConnect 0.4.8 源码核实）：
  · `SimConnect.request_data()` 把 RequestDataOnSimObjectType 的 type/radius 写死为
    SIMCONNECT_SIMOBJECT_TYPE_USER / 0，AircraftRequests 通道永远只读用户机，
    因此这里自建 data definition + request id，并自收
    SIMCONNECT_RECV_ID_SIMOBJECT_DATA_BYTYPE（OBJData.dwData 是 DWORD*8192，
    按各字段 datatype 顺序解包：FLOAT64=8B、INT32=4B、STRING256=256B）。
  · 每帧 BYTYPE 回包只有一个对象，按 (dwRequestID, dwObjectID) 双键累积成表。
  · 线程模型硬约束：SimConnect 实例非线程安全，本 reader 自建独立
    SimConnect(auto_connect=True) 实例（SimConnect 允许多 client 连接同一 sim），
    拥有自己的 dispatch 线程，与 sim_bridge 的遥测连接互不干扰。
  · MSFS 官方文档：dwRadiusMeters 上限 200,000 m（约 108 NM），超出返回
    SIMCONNECT_EXCEPTION_OUT_OF_BOUNDS；0 表示只返回用户机。
  · AI TRAFFIC * 系列在现行文档中缺失但实测可用（DevSupport 官方确认）；
    读取失败时字段留空，绝不影响其它字段。

交通源三态（E11，FSLTL 的 FR24 免费 API 已于 2026-04-30 关闭，injector 一度
退化为纯静态时刻表注入 v1.9.0；2026-05-26 Navigraph 授权后恢复 live 注入）：
  live / static / none —— 依据单位时间内新增/消失的 AI 数量与
  AI TRAFFIC ETA 非空值推断，仅用于日志与 UI 标注。
"""
from __future__ import annotations


import struct
import threading
import time

# MSFS 半径硬上限（米）——超过会触发 SIMCONNECT_EXCEPTION_OUT_OF_BOUNDS
SIMCONNECT_MAX_RADIUS_M = 200_000
_M_PER_NM = 1852.0
# 本实现再自紧一档：默认 40 NM，配置上限 100 NM（≈185 km，留足安全余量）
DEFAULT_RADIUS_NM = 40.0
MAX_RADIUS_NM = 100.0

# 字段表：(SimVar 名, 单位, dtype) —— 顺序即解包顺序，不可调换。
# 字符串统一用 STRING256（AI TRAFFIC * 在 SDK 里是 string32，256 同样能收；
# 换成 string32 需同步改 _DTYPE_BYTES 与 _DATATYPE_ENUM）。
FIELD_TABLE = [
    ("PLANE LATITUDE", "degrees", "float64"),
    ("PLANE LONGITUDE", "degrees", "float64"),
    ("PLANE ALTITUDE", "feet", "float64"),
    ("GROUND VELOCITY", "knots", "float64"),
    ("VERTICAL SPEED", "feet per minute", "float64"),
    ("PLANE HEADING DEGREES TRUE", "degrees", "float64"),
    ("SIM ON GROUND", "bool", "int32"),
    ("ATC ID", "string", "string256"),
    ("ATC TYPE", "string", "string256"),
    ("ATC MODEL", "string", "string256"),
    ("AI TRAFFIC ASSIGNED RUNWAY", "string", "string256"),
    ("AI TRAFFIC ASSIGNED PARKING", "string", "string256"),
    ("AI TRAFFIC CURRENT ICAO", "string", "string256"),
    ("AI TRAFFIC ISIFR", "bool", "int32"),
    ("AI TRAFFIC FROMAIRPORT", "string", "string256"),
    ("AI TRAFFIC TOAIRPORT", "string", "string256"),
    ("AI TRAFFIC ETA", "seconds", "float64"),
]

_DTYPE_BYTES = {"float64": 8, "int32": 4, "string256": 256, "string32": 32}

# 交通源形态
SOURCE_LIVE = "live"
SOURCE_STATIC = "static"
SOURCE_NONE = "none"
SOURCE_UNAVAILABLE = "unavailable"

# ICAO 机型 → 尾流类别（依据 ICAO Doc 4444 Amd.9 的最大认证起飞重量判据；
# B757 按 FAA 规定视作 Heavy，即使 MTOW 落在 Medium 区间）
_SUPER_TYPES = {"A388"}
_HEAVY_TYPES = {
    "A306", "A332", "A333", "A339", "A343", "A345", "A346", "A359", "A35K",
    "B741", "B742", "B743", "B744", "B748", "B763", "B764", "B772", "B773",
    "B77L", "B77W", "B778", "B779", "B752",  # B752=B757，FAA 规则按 Heavy
}
_MEDIUM_TYPES = {
    "A223", "A318", "A319", "A320", "A321", "A20N", "A21N", "A22N",
    "B732", "B733", "B734", "B735", "B736", "B737", "B738", "B739", "B37M",
    "B38M", "B39M", "B733", "B732", "B720", "B721", "B722", "B727", "B732",
    "E135", "E145", "E170", "E175", "E190", "E195", "E75L", "E75S", "E290",
    "CRJ1", "CRJ2", "CRJ7", "CRJ9", "CRJX", "DH8A", "DH8B", "DH8C", "DH8D",
    "F70", "F100", "MD81", "MD82", "MD83", "MD87", "MD88", "MD90", "YK42",
}
_LIGHT_TYPES = {
    "C172", "C182", "C206", "C208", "C25A", "C25B", "C25C", "C510", "C56X",
    "PA28", "PA31", "PA44", "BE58", "BE20", "BE99", "TBM7", "TBM8", "TBM9",
    "PC12", "DA40", "DA42", "DA62", "SR20", "SR22", "GLF4", "GLF5", "GLF6",
    "E55P", "E545", "FA50", "FA7X", "FA8X", "CL60", "HA4T",
}


def compute_radius_m(config=None) -> int:
    """按配置计算 SimConnect 半径（米），clamp 到 ≤100 NM 且 ≥1 m（0 的语义是
    "只回用户机"，这里不允许：交通枚举需要覆盖周边 AI）。"""
    config = config or {}
    nav_cfg = config.get("navdata", {}) or {}
    traf_cfg = config.get("traffic", {}) or {}
    raw = traf_cfg.get("traffic_radius_nm", nav_cfg.get("traffic_radius_nm", DEFAULT_RADIUS_NM))
    try:
        radius_nm = float(raw)
    except (TypeError, ValueError):
        radius_nm = DEFAULT_RADIUS_NM
    radius_nm = max(0.1, min(radius_nm, MAX_RADIUS_NM))
    return max(int(radius_nm * _M_PER_NM), 1)


def wake_category_for_type(icao_type) -> str:
    """ICAO 机型标识 → 尾流类别（H/M/L/J）；未知返回 'UNKNOWN'。"""
    text = str(icao_type or "").strip().upper()
    if not text:
        return "UNKNOWN"
    if text in _SUPER_TYPES:
        return "J"
    if text in _HEAVY_TYPES:
        return "H"
    if text in _MEDIUM_TYPES:
        return "M"
    if text in _LIGHT_TYPES:
        return "L"
    # 前缀粗分（ATC TYPE 可能带子型号后缀）
    for table, category in ((_SUPER_TYPES, "J"), (_HEAVY_TYPES, "H"),
                            (_MEDIUM_TYPES, "M"), (_LIGHT_TYPES, "L")):
        if any(text.startswith(t[:3]) and t[:3] == text[:3] for t in table):
            return category
    return "UNKNOWN"


_FIELD_DTYPE = {name: dtype for name, _unit, dtype in FIELD_TABLE}
_FIELD_ORDER = [name for name, _unit, _dtype in FIELD_TABLE]


def _decode_record(values_bytes: bytes, field_names):
    """按 FIELD_TABLE 顺序解包一段 dwData 字节。失败字段留 None。"""
    out = {}
    offset = 0
    for name in field_names:
        dtype = _FIELD_DTYPE[name]
        size = _DTYPE_BYTES[dtype]
        chunk = values_bytes[offset:offset + size]
        if len(chunk) < size:
            out[name] = None          # 缓冲区不足：该字段留空
        else:
            try:
                if dtype == "float64":
                    out[name] = struct.unpack_from("<d", chunk)[0]
                elif dtype == "int32":
                    out[name] = struct.unpack_from("<i", chunk)[0]
                else:
                    out[name] = chunk.split(b"\x00", 1)[0].decode("ascii", "ignore")
            except (struct.error, ValueError):
                out[name] = None
        offset += size
    return out


def _make_traffic_simconnect_class():
    """惰性构造 SimConnect 子类（仅在真机、装了 SimConnect 时导入）。

    为什么需要子类：python-SimConnect 的分发回调（my_dispatch_proc_rd →
    SIMCONNECT_RECV_ID_SIMOBJECT_DATA_BYTYPE）只会调用 SimConnect 实例自身
    的 `handle_simobject_event`。本 reader 是独立类，必须让真正连着 sim 的
    那个实例的回调转发进来，否则回包无人接收、`_table` 恒空（C1 根因）。

    子类持一个 `_traffic_sink`：reader 在 start() 里把自己挂上去，收到
    的每个 BYTYPE 包先转给 reader；未挂（未启动/已停止）时退回基类默认
    实现，保证库自身行为不丢。测试可用 `traffic_sm_factory` 注入替身。
    """
    from SimConnect import SimConnect

    class _TrafficSimConnect(SimConnect):
        # reader 在 start() 里把自身挂到 _traffic_sink
        _traffic_sink = None

        def handle_simobject_event(self, ObjData):
            sink = self._traffic_sink
            if sink is not None:
                sink.handle_simobject_event(ObjData)
            else:
                super().handle_simobject_event(ObjData)

    return _TrafficSimConnect


class SimConnectTrafficReader:
    """自建 SimConnect 连接枚举周边 AI 飞机，累积成表。

    用法：
        reader = SimConnectTrafficReader(config)
        reader.start()             # 连接失败则 available=False，调用方降级
        targets = reader.poll_once()   # list[dict]，无新数据返回 []

    线程模型（已对照 0.4.8 源码核实）：基类无后台 dispatch 线程，
    start() 成功后由本 reader 自建 daemon 线程泵 CallDispatch；
    stop() 先停线程、后关句柄。
    """

    # _table 老化时间（秒）：BYTYPE 请求 2 Hz，对象离场后不再回包；留足
    # 20 个周期余量（半径 100 NM 多对象枚举时首帧可能有延迟）再剔除。
    _TABLE_TTL_SECONDS = 10.0

    def __init__(self, config=None, connect_fn=None, traffic_sm_factory=None):
        self.config = config or {}
        self._connect_fn = connect_fn
        self._traffic_sm_factory = traffic_sm_factory or _make_traffic_simconnect_class
        self._sm = None
        self._thread = None
        self._stop_event = threading.Event()
        self._available = False
        self._last_error = None
        self._lock = threading.Lock()
        # (request_id, object_id) -> 最近一帧解码值
        self._table = {}
        self._dirty = False
        self._request_id = None
        self._def_id = None
        # 交通源形态推断窗口
        self._seen = {}            # callsign -> last_seen_ts
        self._window_start = time.time()
        self._window_added = 0
        self._window_removed = 0
        self._window_eta_seen = False
        self._source_state = SOURCE_UNAVAILABLE

    # ── 生命周期 ────────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return self._available

    @property
    def source_state(self) -> str:
        return self._source_state

    @property
    def last_error(self):
        return self._last_error

    def start(self) -> bool:
        """建立独立 SimConnect 连接并注册数据定义。失败返回 False（不抛）。"""
        if self._available:
            return True
        try:
            sm = self._connect_simconnect()
        except Exception as e:
            self._last_error = str(e)
            self._available = False
            self._source_state = SOURCE_UNAVAILABLE
            print(f"SimConnectTrafficReader: unavailable — {e}")
            return False
        try:
            if not hasattr(sm.dll, "RequestDataOnSimObjectType"):
                raise AttributeError("SimConnect.dll 缺少 RequestDataOnSimObjectType")
            self._sm = sm
            if hasattr(sm, "_traffic_sink"):
                sm._traffic_sink = self          # 子类实例把回调转发给本 reader
            self._def_id = sm.new_def_id()
            self._request_id = sm.new_request_id()
            for name, unit, dtype_name in FIELD_TABLE:
                dtype = _DATATYPE_ENUM(dtype_name)
                sm.dll.AddToDataDefinition(
                    sm.hSimConnect, self._def_id.value,
                    name.encode("ascii"), (unit or "").encode("ascii"),
                    dtype, 0, _simconnect_unused())
            self._start_dispatch()                # 0.4.8 基类无后台 dispatch 线程，必须自建
            self._available = True
            print("SimConnectTrafficReader: connected (independent SimConnect client)")
            return True
        except Exception as e:
            self._last_error = str(e)
            self._available = False
            self._source_state = SOURCE_UNAVAILABLE
            print(f"SimConnectTrafficReader: init failed — {e}")
            self._stop_dispatch()
            self._safe_exit()
            return False

    def _connect_simconnect(self):
        if self._connect_fn is not None:
            return self._connect_fn()            # 测试注入：假实例
        # 0.4.8 connect() 在 MSFS 未运行时走 `except OSError: exit(0)`，
        # 而 exit() 抛 SystemExit（BaseException，穿透 except Exception），
        # 会静默杀死整条交通扫描线程（见文件头根因）。此处显式兜 SystemExit。
        try:
            return self._traffic_sm_factory()(auto_connect=True)
        except SystemExit:
            raise ConnectionError("SimConnect exited during connect (MSFS not running?)")

    # ── dispatch 线程（0.4.8 基类无后台线程，connect() 返回后全库再无
    #    CallDispatch 调用点；必须自建，否则回包无人接收）。做进 commit 的
    #    版本决策：锁死 SimConnect==0.4.8 + 自建线程；若将来升级 pin 到带
    #    timerThread 的版本，删掉这段，否则两个线程对同一 hSimConnect 重入。──

    def _start_dispatch(self):
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._dispatch_loop, name="SimConnectTrafficDispatch", daemon=True)
        self._thread.start()

    def _stop_dispatch(self):
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
            self._thread = None

    def _dispatch_loop(self):
        sm = self._sm
        dll = getattr(sm, "dll", None)
        if sm is None or not hasattr(sm, "my_dispatch_proc_rd") \
                or dll is None or not hasattr(dll, "CallDispatch"):
            return                       # 不完整客户端（如测试替身）：无可泵
        while not self._stop_event.is_set():
            try:
                sm.dll.CallDispatch(sm.hSimConnect, sm.my_dispatch_proc_rd, None)
            except OSError:
                break                   # 句柄已关（stop 竞态或 sim 退出）：直接退
            except Exception as e:
                self._last_error = str(e)
            time.sleep(0.02)

    def _safe_exit(self):
        sm, self._sm = self._sm, None
        if sm is not None:
            try:
                sm.exit()
            except Exception:
                pass

    def stop(self):
        self._stop_dispatch()           # 先停线程、后关句柄，避免对已 Close 的
        self._available = False         # handle 调 CallDispatch
        self._safe_exit()

    # ── 数据请求与接收 ──────────────────────────────────────────────────────

    def request_once(self):
        """周期性请求（traffic_manager 的扫描循环调用）。半径按配置 clamp。"""
        if not self._available or self._sm is None:
            return False
        try:
            obj_type = _simobject_type("AIRCRAFT")
            radius_m = compute_radius_m(self.config)
            self._sm.dll.RequestDataOnSimObjectType(
                self._sm.hSimConnect, self._request_id.value, self._def_id.value,
                radius_m, obj_type)
            return True
        except Exception as e:
            print(f"SimConnectTrafficReader: request failed — {e}")
            self._last_error = str(e)
            return False

    def handle_simobject_event(self, ObjData):
        """覆写 python-SimConnect 的默认分发：按 (RequestID, ObjectID) 双键路由，
        按 len(FIELD_TABLE) 逐字段解包（默认实现只取 definitions[0]，读不出
        多字段定义，更处理不了字符串字段）。"""
        try:
            request_id = ObjData.dwRequestID
            object_id = ObjData.dwObjectID
            raw = bytes(ObjData.dwData)
            values = _decode_record(raw, _FIELD_ORDER)
            values["_object_id"] = object_id
            values["_last_seen"] = time.time()
            with self._lock:
                self._table[(request_id, object_id)] = values
                self._dirty = True
        except Exception as e:
            print(f"SimConnectTrafficReader: decode failed — {e}")

    def poll_once(self) -> list:
        """取走累积表中的当前快照（每帧 BYTYPE 只有单对象，故需累积）。

        同时做两件事：
          · 老化剔除（§P0-5）：BYTYPE 请求是周期性的，对象离场后不再回包，
            超过 `_TABLE_TTL_SECONDS` 未刷新的条目从 `_table` 剔除——
            否则离场 AI 会残留（每条约 32KB 缓冲区级别的解包字典），
            且会被反复塞回 traffic_manager 的跟踪表。
          · 交通源形态推断。返回的 dict 已含 traffic_manager 需要的键；
            无任何 AI 时返回空列表。
        """
        if not self._available:
            return []
        self.request_once()
        with self._lock:
            self._evict_stale(time.time())
            snapshot = list(self._table.values())
        self._refresh_source_state(snapshot)
        return [self._to_traffic_dict(v) for v in snapshot]

    def _evict_stale(self, now):
        """剔除超过老化时间未刷新的 (request_id, object_id) 条目。"""
        cutoff = now - self._TABLE_TTL_SECONDS
        stale = [key for key, values in self._table.items()
                 if values.get("_last_seen", 0.0) < cutoff]
        for key in stale:
            del self._table[key]

    # ── 形态推断（E11） ──────────────────────────────────────────────────────

    def _refresh_source_state(self, snapshot):
        now = time.time()
        current = {}
        for values in snapshot:
            callsign = (values.get("ATC ID") or "").strip().upper()
            if callsign:
                current[callsign] = now
                if values.get("AI TRAFFIC ETA"):
                    self._window_eta_seen = True
        if now - self._window_start >= 60.0:
            for callsign, last_seen in list(self._seen.items()):
                if callsign not in current:
                    self._window_removed += 1
            self._seen = current
            self._window_start = now
            added_removed = self._window_added + self._window_removed
            if not current:
                self._source_state = SOURCE_NONE
            elif self._window_eta_seen and added_removed >= 2:
                self._source_state = SOURCE_LIVE
            else:
                self._source_state = SOURCE_STATIC
            self._window_added = 0
            self._window_removed = 0
            self._window_eta_seen = False
            print(f"SimConnectTrafficReader: traffic source state → {self._source_state}")
        else:
            for callsign in current:
                if callsign not in self._seen:
                    self._window_added += 1
            self._seen = current

    # ── 输出适配 ────────────────────────────────────────────────────────────

    @staticmethod
    def _to_traffic_dict(values: dict) -> dict:
        callsign = (values.get("ATC ID") or "").strip().upper()
        ac_type = (values.get("ATC TYPE") or "").strip().upper()
        eta = values.get("AI TRAFFIC ETA")
        return {
            "callsign": callsign,
            "latitude": values.get("PLANE LATITUDE"),
            "longitude": values.get("PLANE LONGITUDE"),
            "altitude": values.get("PLANE ALTITUDE"),
            "heading": values.get("PLANE HEADING DEGREES TRUE"),
            "airspeed": values.get("GROUND VELOCITY"),
            "vertical_speed": values.get("VERTICAL SPEED"),
            "on_ground": bool(values.get("SIM ON GROUND") or 0),
            "aircraft_type": ac_type,
            "wake_category": wake_category_for_type(ac_type),
            "assigned_runway": (values.get("AI TRAFFIC ASSIGNED RUNWAY") or "").strip().upper() or None,
            "assigned_parking": (values.get("AI TRAFFIC ASSIGNED PARKING") or "").strip().upper() or None,
            "icao_dest": (values.get("AI TRAFFIC TOAIRPORT") or "").strip().upper() or None,
            "icao_origin": (values.get("AI TRAFFIC FROMAIRPORT") or "").strip().upper() or None,
            "current_icao": (values.get("AI TRAFFIC CURRENT ICAO") or "").strip().upper() or None,
            "is_ifr": bool(values.get("AI TRAFFIC ISIFR") or 0),
            "eta_s": float(eta) if isinstance(eta, (int, float)) else None,
            "object_id": values.get("_object_id"),
        }


# dtype 名 → SIMCONNECT_DATATYPE 枚举值。装了 SimConnect 时用官方 Enum，
# 没装（开发/测试机）时用 ARINC 424 SDK 常量直填，保证模块可用可测。
_DATATYPE_VALUES = {"int32": 1, "int64": 2, "float32": 3, "float64": 4,
                    "string8": 5, "string32": 6, "string64": 7, "string128": 8,
                    "string256": 9, "string260": 10}
_SIMCONNECT_UNUSED_VALUE = 0xFFFFFFFF


def _DATATYPE_ENUM(dtype_name: str):
    """把字段表里的 type 名映射到 SimConnect SIMCONNECT_DATATYPE 值。"""
    try:
        from SimConnect.Enum import SIMCONNECT_DATATYPE
        return SIMCONNECT_DATATYPE[f"SIMCONNECT_DATATYPE_{dtype_name.upper()}"]
    except Exception:
        return _DATATYPE_VALUES[dtype_name]


def _simconnect_unused():
    try:
        from SimConnect.Constants import SIMCONNECT_UNUSED
        return SIMCONNECT_UNUSED
    except Exception:
        return _SIMCONNECT_UNUSED_VALUE


def _simobject_type(name: str) -> int:
    """SIMCONNECT_SIMOBJECT_TYPE 枚举值；USER=0/ALL=1/AIRCRAFT=2/HELICOPTER=3。"""
    try:
        from SimConnect.Enum import SIMCONNECT_SIMOBJECT_TYPE
        return SIMCONNECT_SIMOBJECT_TYPE[f"SIMCONNECT_SIMOBJECT_TYPE_{name}"]
    except Exception:
        return {"USER": 0, "ALL": 1, "AIRCRAFT": 2, "HELICOPTER": 3,
                "BOAT": 4, "GROUND": 5}[name]
