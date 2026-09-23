"""
spike_owned_ai.py — P0 可行性 Spike（见 docs/hybrid-owned-traffic-plan.md §4 P0）。

⚠️ 这是**手动实机脚本，不是自动测试**，也不在 CI 里跑。
    它唯一的目的：在真实 MSFS（建议同时开着 FSLTL）上确认下面几个 SimConnect
    调用真的存在、签名正确、行为符合假设。**离线环境（无 MSFS）会在 connect()
    直接失败退出——这是预期行为。**

不要把本脚本的任何输出当成"已验证结论"。跑完后，把每个 API 的实测结果（存在与否、
返回、object id、坑）**手动誊抄**回 docs/hybrid-owned-traffic-plan.md §9「P0 实测
结论」。誊抄的必须是你亲眼在 MSFS 里看到的，不是脚本的乐观打印。

用法（Windows，MSFS 正在运行）：
    python spike_owned_ai.py caps              # DLL 能力探测（最安全，先跑）
    python spike_owned_ai.py probe             # ★诊断模式：OPEN包+用户位置+3创建API×多title矩阵
    python spike_owned_ai.py spawn             # 创建一架并拿 objectID，10s 后移除
    python spike_owned_ai.py plan  <PLN>       # A 路径：ParkedATC+飞行计划（正确姿势，
                                              #   先 set OF_AIRPORT=<当前机场ICAO>；计划须从该机场出发）
    python spike_owned_ai.py plan_nonatc <PLN> # 对照：NonATC+计划（P0-10 已证原地不动）
    python spike_owned_ai.py drive             # spawn + AIReleaseControl + 逐帧写位置（B 路径）

probe 模式是给"spawn 返回 E_FAIL 但不明原因"准备的：它会依次尝试
AICreateNonATCAircraft / AICreateParkedATCAircraft / AICreateSimulatedObject
三个创建 API × 若干机模 title，报告每种组合的 HRESULT / ASSIGNED_OBJECT_ID /
EXCEPTION，成功的立即清理。据此可分辨：title 无效？主菜单未进世界？API 被禁？

可选环境变量：
    OF_LAT / OF_LON / OF_ALT_FT / OF_HDG   初始位置（默认 KJFK 跑道口附近）
    OF_TITLE                               覆盖首选机模 title
    OF_AIRPORT                             停机位探测用机场（默认 KJFK）

设计对齐 core/simconnect_traffic.py：直接打 sc.dll.*（python-SimConnect 未封装
这些 AI 创建函数），自建独立 SimConnect 实例 + 自建 CallDispatch 泵。

**P0 三轮实测教训（2026-09-22，已修复到本脚本）**：
1. python-SimConnect 库 Attributes.py 给 AICreateNonATCAircraft 声明了
   argtypes，InitPos 参数必须是**库自己的** SIMCONNECT_DATA_INITPOSITION
   类实例——本地另定义同名类，调用时
   `ArgumentError: expected SIMCONNECT_DATA_INITPOSITION instance instead of
   SIMCONNECT_DATA_INITPOSITION`（同名不同类，ctypes 查类身份）。
   修复：从 SimConnect.Enum 导入库类构造（_initpos_class()）。
2. 0.4.8 还把 title/tail 两个 c_char_p 误声明为 c_double（bytes 直接
   ArgumentError）。修复：connect() 后改写 AICreateNonATCAircraft.argtypes
   为 SDK 正确签名（_repair_argtypes）。
3. ID 一律传纯 int（DWORD 实例传给库 CtypesEnum from_param 在某些
   Python 版本上会炸）。
4. CallDispatch 的回调包装类型同样只认库自己的 DispatchProc：
   本地 WINFUNCTYPE 同款签名是另一个原型缓存类 →
   `expected WinFunctionType instance instead of WinFunctionType`。
   修复：type(sc.my_dispatch_proc_rd) 包装我们的回调（_make_dispatch_proc）。
5. spawn 返回 HRESULT=-2147467259（0x80004005=E_FAIL）且无 EXCEPTION 回包
   ——原因未明，probe 模式用于定位（主菜单未进世界 / title 不在装机列表 /
   创建 API 在该 sim 上不可用三选一）。
6. **标题（title）是 MSFS2024 上的关键坑（P0-7 实测结论）**：
   "Airbus A320 Neo Asobo" 等 Asobo 默认机容器名在部分 MSFS2024 装机
   里不存在 → CREATE_OBJECT_FAILED(22)。**可用 title = 用户当前飞机的
   `TITLE` simvar 值**（如 'A350-900 (Default Cabin)'，涂装式容器名）。
   `set OF_TITLE=那个值` 后重试即可。probe v2 的 title 候选已含此来源。
"""
from __future__ import annotations

import ctypes
import os
import sys
import time

# ── SDK 常量（RECV id）。以官方 SIMCONNECT_RECV_ID 枚举为准；若实测不符，
#    以实测为准。──
RECV_ID_EXCEPTION = 1
RECV_ID_OPEN = 2
RECV_ID_QUIT = 3
RECV_ID_SIMOBJECT_DATA = 8
RECV_ID_ASSIGNED_OBJECT_ID = 12

# 目标 DLL 函数（本 spike 要验证的全部对象）
TARGET_FUNCS = [
    "AICreateNonATCAircraft",
    "AICreateSimulatedObject",
    "AICreateParkedATCAircraft",
    "AISetAircraftFlightPlan",
    "AIReleaseControl",
    "AIRemoveObject",
    "SetDataOnSimObject",
    "CallDispatch",
]

# SIMCONNECT_EXCEPTION 码表（官方 Enum；收到 EXCEPTION 回包时翻译给人看）
EXCEPTION_NAMES = {
    0: "NONE", 1: "ERROR", 2: "SIZE_MISMATCH", 3: "UNRECOGNIZED_ID",
    4: "UNOPENED", 5: "VERSION_MISMATCH", 11: "TOO_MANY_OBJECTS",
    22: "CREATE_OBJECT_FAILED(创建失败：主菜单未进世界/title无效/位置无效)",
    23: "LOAD_FLIGHTPLAN_FAILED(飞行计划载入失败：路径无效)",
    24: "OPERATION_INVALID_FOR_OBJECT_TYPE", 31: "OUT_OF_BOUNDS",
    33: "OBJECT_OUTSIDE_REALITY_BUBBLE(超出现实气泡)",
    34: "OBJECT_CONTAINER(机模 title 不在本机装机列表)",
    35: "OBJECT_AI", 36: "OBJECT_ATC",
}

# ── ctypes 结构（按 MSFS SDK 定义；[需实机验证] 字段顺序/对齐）──
# 注意：真正传 InitPos 时用 SimConnect.Enum 里的库类（_initpos_class）——
# 库给 AICreateNonATCAircraft 声明了 argtypes，本地同名类过不了身份校验。
DWORD = ctypes.c_uint32


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


class SIMCONNECT_RECV_OPEN(ctypes.Structure):
    _fields_ = [
        ("dwSize", DWORD), ("dwVersion", DWORD), ("dwID", DWORD),
        ("szApplicationName", ctypes.c_char * 256),
        ("dwApplicationVersionMajor", DWORD),
        ("dwApplicationVersionMinor", DWORD),
        ("dwApplicationBuildMajor", DWORD),
        ("dwApplicationBuildMinor", DWORD),
        ("dwSimConnectVersionMajor", DWORD),
        ("dwSimConnectVersionMinor", DWORD),
        ("dwSimConnectBuildMajor", DWORD),
        ("dwSimConnectBuildMinor", DWORD),
        ("dwReserved1", DWORD),
        ("dwReserved2", DWORD),
    ]


class SIMCONNECT_RECV_ASSIGNED_OBJECT_ID(ctypes.Structure):
    _fields_ = [
        ("dwSize", DWORD), ("dwVersion", DWORD), ("dwID", DWORD),
        ("dwRequestID", DWORD), ("dwObjectID", DWORD),
    ]


class SIMCONNECT_RECV_EXCEPTION(ctypes.Structure):
    # 字段序列对齐 python-SimConnect Enum.py（MSFS SDK）：头 3 DWORD 后 5 个。
    # P0 probe 实测：偏移 16 处的 UNKNOWN_SENDID 装的其实是**发送包 id**
    # （SimConnect_GetLastSentPacketID 的返回值；库 RequestList/SimConnect.py
    # 正是用它关联异常），不是我们的 request id。dwSendID 位语义随版本变化，
    # 两个都试。之前少定义两个 DWORD，dwSendID 读到的其实是发包 id，导致
    # 异常全部关联不上（矩阵里 exc=None 的来源）。
    _fields_ = [
        ("dwSize", DWORD), ("dwVersion", DWORD), ("dwID", DWORD),
        ("dwException", DWORD),
        ("UNKNOWN_SENDID", DWORD),
        ("dwSendID", DWORD),
        ("UNKNOWN_INDEX", DWORD),
        ("dwIndex", DWORD),
    ]


class SIMCONNECT_RECV_SIMOBJECT_DATA(ctypes.Structure):
    # 对齐库 Enum.py：dwData 前有 dwentrynumber/dwoutof/dwDefineCount 三个
    # DWORD——漏掉它们 dwData 整体偏移 12 字节，float64 全解错（P0 probe
    # 用户位置解出 lat=0/lon=3e118 的根因）。
    _fields_ = [
        ("dwSize", DWORD), ("dwVersion", DWORD), ("dwID", DWORD),
        ("dwRequestID", DWORD), ("dwObjectID", DWORD),
        ("dwDefineID", DWORD), ("dwFlags", DWORD),
        ("dwentrynumber", DWORD), ("dwoutof", DWORD), ("dwDefineCount", DWORD),
        ("dwData", DWORD * 8192),
    ]


# void CALLBACK DispatchProc(SIMCONNECT_RECV* pData, DWORD cbData, void* pContext)
if sys.platform == "win32":
    DispatchProcType = ctypes.WINFUNCTYPE(
        None, ctypes.POINTER(SIMCONNECT_RECV), DWORD, ctypes.c_void_p)
else:  # 仅为在非 Windows 上能 import 本模块（不会真跑）
    DispatchProcType = ctypes.CFUNCTYPE(
        None, ctypes.POINTER(SIMCONNECT_RECV), DWORD, ctypes.c_void_p)


# 捕获到的回包（回包 → pending 结果表）
_ASSIGNED: dict[int, int] = {}     # request_id -> object_id
_EXCEPTIONS: list[tuple[int, int]] = []   # (dwException, dwSendID)
_USER_POS: dict[int, tuple] = {}   # request_id -> (lat, lon, alt)
_USER_TITLE: dict[int, str] = {}   # request_id -> 用户机容器 title


def _handle_packet(pData):
    """回包处理（自建泵与库后台泵共用，见 _install_dispatch_chain）。"""
    try:
        dwid = pData.contents.dwID
        if dwid == RECV_ID_ASSIGNED_OBJECT_ID:
            rec = ctypes.cast(
                pData, ctypes.POINTER(SIMCONNECT_RECV_ASSIGNED_OBJECT_ID)).contents
            _ASSIGNED[rec.dwRequestID] = rec.dwObjectID
            print(f"  [recv] ASSIGNED_OBJECT_ID req={rec.dwRequestID} obj={rec.dwObjectID}")
        elif dwid == RECV_ID_EXCEPTION:
            rec = ctypes.cast(
                pData, ctypes.POINTER(SIMCONNECT_RECV_EXCEPTION)).contents
            # P0 probe 实测：异常按"发送包 id"关联（偏移 16 位），
            # dwSendID 位语义随版本变化，两个都记录
            send_id = rec.UNKNOWN_SENDID or rec.dwSendID
            _EXCEPTIONS.append((rec.dwException, send_id))
            name = EXCEPTION_NAMES.get(rec.dwException, "?")
            print(f"  [recv] EXCEPTION code={rec.dwException}({name}) "
                  f"sendID(packet)={send_id}")
        elif dwid == RECV_ID_OPEN:
            rec = ctypes.cast(
                pData, ctypes.POINTER(SIMCONNECT_RECV_OPEN)).contents
            app = bytes(rec.szApplicationName).split(b"\x00", 1)[0].decode(
                "ascii", "ignore")
            print(f"  [recv] OPEN app={app!r} "
                  f"app_ver={rec.dwApplicationVersionMajor}."
                  f"{rec.dwApplicationVersionMinor}."
                  f"{rec.dwApplicationBuildMajor}."
                  f"{rec.dwApplicationBuildMinor} "
                  f"SimConnect_ver={rec.dwSimConnectVersionMajor}."
                  f"{rec.dwSimConnectVersionMinor}."
                  f"{rec.dwSimConnectBuildMajor}."
                  f"{rec.dwSimConnectBuildMinor}")
        elif dwid == RECV_ID_SIMOBJECT_DATA:
            rec = ctypes.cast(
                pData, ctypes.POINTER(SIMCONNECT_RECV_SIMOBJECT_DATA)).contents
            # 布局：3×float64 (24B) + string256 (256B)
            raw = ctypes.string_at(ctypes.byref(rec.dwData), 24 + 256)
            vals = ctypes.cast(raw, ctypes.POINTER(
                ctypes.c_double * 3)).contents
            title = raw[24:].split(b"\x00", 1)[0].decode("ascii", "ignore")
            _USER_POS[rec.dwRequestID] = tuple(vals)
            if title:
                _USER_TITLE[rec.dwRequestID] = title
    except Exception as e:  # noqa: BLE001 — spike 里只打印，不中断泵
        print(f"  [recv] dispatch parse error: {e}")


def _make_dispatch_proc(sc):
    """构造 dispatch 回调并返回。

    P0 第三轮教训：包装类型必须用库自己的 DispatchProc
    （type(sc.my_dispatch_proc_rd)）——本地 WINFUNCTYPE 同款签名是另一个
    原型缓存类（库签名里的 POINTER(SIMCONNECT_RECV) 指向库模块的 RECV
    类），过不了 CallDispatch 的 argtypes 身份校验。
    """
    def _proc(pData, cbData, pContext):
        _handle_packet(pData)

    base = getattr(sc, "my_dispatch_proc_rd", None)
    proc_cls = type(base) if base is not None else DispatchProcType
    return proc_cls(_proc)


def _install_dispatch_chain(sc):
    """新版库（如 0.4.26）connect() 会起后台 timerThread 持续 CallDispatch
    泵 my_dispatch_proc_rd，与我们自建的泵竞争同一消息队列——回包可能被库
    线程赢走（库只打印/塞环境变量）。这里包一层库的 my_dispatch_proc 并用
    库类型重建 my_dispatch_proc_rd，两条路径都先过 _handle_packet。
    0.4.8 无后台线程，仅自建泵工作，包装无副作用。
    """
    original = getattr(sc, "my_dispatch_proc", None)
    if original is None:
        return

    def _chained(pData, cbData, pContext):
        _handle_packet(pData)
        return original(pData, cbData, pContext)

    sc.my_dispatch_proc = _chained
    proc_type = type(getattr(sc, "my_dispatch_proc_rd", None))
    if proc_type is not None:
        try:
            sc.my_dispatch_proc_rd = proc_type(_chained)
        except Exception as e:  # noqa: BLE001
            print(f"  [fix] dispatch chain 重建失败（退回自建泵）：{e!r}")


def connect():
    """建立独立 SimConnect（对齐 simconnect_traffic 的 auto_connect 模式）。"""
    try:
        from SimConnect import SimConnect
    except ImportError:
        sys.exit("SimConnect 库未安装（pip install SimConnect）。本 spike 只能在装了库的 Windows 上跑。")
    try:
        sc = SimConnect(auto_connect=True)
    except SystemExit:
        sys.exit("SimConnect 在 connect 阶段 exit —— MSFS 没在运行？（离线预期结果）")
    print("已连接 SimConnect（独立 client）。")
    _repair_argtypes(sc)
    return sc


def _initpos_class():
    """取库自己的 SIMCONNECT_DATA_INITPOSITION 类（P0 教训 #1）。

    库 Attributes.py 给 AICreateNonATCAircraft 声明了 argtypes，结构体
    参数必须是库那个类对象的实例；本地同名类过不了 ctypes 身份校验
    （报错信息里两个类名一模一样，极具迷惑性）。导入失败退回本地定义
    （布局一致，仅无 argtypes 校验场景）。
    """
    try:
        from SimConnect.Enum import SIMCONNECT_DATA_INITPOSITION as cls
        return cls
    except Exception:
        return SIMCONNECT_DATA_INITPOSITION


def _repair_argtypes(sc):
    """修正 python-SimConnect 0.4.8 对 AICreateNonATCAircraft 的 argtypes bug：
    title/tail 两个 c_char_p 被误声明为 c_double（P0 教训 #2）。
    新版库已是 c_char_p，这里重写为 SDK 正确签名，无害且幂等。"""
    try:
        cls = _initpos_class()
        sc.dll.AICreateNonATCAircraft.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, cls,
            ctypes.c_uint32]
        print("  [fix] AICreateNonATCAircraft argtypes → SDK 正确签名")
    except Exception as e:
        print(f"  [fix] argtypes 改写失败（不影响继续，调用时再看报错）：{e!r}")


def _init_position(user_pos=None) -> SIMCONNECT_DATA_INITPOSITION:
    """初始位置构造。优先级：OF_LAT/OF_LON 环境变量 > 用户机位置 + 北偏
    OF_OFFSET_NM 海里 > KJFK 默认。

    P0 probe 教训：创建点必须在用户已加载区域（现实气泡）内，否则
    NonATC 报 CREATE_OBJECT_FAILED、ParkedATC 报 OBJECT_OUTSIDE_REALITY_BUBBLE。

    OF_OFFSET_NM（默认 1.8）：相对用户机向北偏移多少海里。想"一眼看到
    生成机"就把它设小（如 0.3 ≈ 550m），否则 1.8 海里在座舱视野外。
    """
    cls = _initpos_class()
    if user_pos:
        offset_nm = float(os.environ.get("OF_OFFSET_NM", "1.8"))
        # 用户北侧 offset_nm 海里处错开，避免和玩家机重叠
        default_lat, default_lon = user_pos[0] + offset_nm / 60.0, user_pos[1]
        default_alt = max(user_pos[2], 0.0)
    else:
        default_lat, default_lon = 40.6413, -73.7781   # KJFK
        default_alt = 13.0
    return cls(
        Latitude=float(os.environ.get("OF_LAT", default_lat)),
        Longitude=float(os.environ.get("OF_LON", default_lon)),
        Altitude=float(os.environ.get("OF_ALT_FT", default_alt)),
        Pitch=0.0, Bank=0.0,
        Heading=float(os.environ.get("OF_HDG", "310")),
        OnGround=1, Airspeed=0,
    )


_PROBE_PUMP_SECONDS = 1.2   # probe 每轮等待回包（本地 SimConnect 足够）


def _pump(sc, proc, seconds: float):
    """在 seconds 秒内持续 CallDispatch，收 assigned id / exception。"""
    end = time.time() + seconds
    while time.time() < end:
        try:
            sc.dll.CallDispatch(sc.hSimConnect, proc, None)
        except OSError:
            break
        except Exception as e:  # noqa: BLE001 — 回调类型/签名不对时别再刷屏
            print(f"  [pump] CallDispatch 失败（回调类型不匹配？）：{e!r}")
            break
        time.sleep(0.02)


def _last_packet_id(sc):
    """SimConnect_GetLastSentPacketID——刚发出的调用的发送包 id。

    P0 probe 实测：EXCEPTION 回包按发包 id 关联请求（不是 request id）。
    拿不到（旧版库无此函数）返回 None，调用方退回 request id 匹配。
    """
    getter = getattr(sc.dll, "GetLastSentPacketID", None)
    if getter is None:
        return None
    try:
        out = ctypes.c_uint32(0)
        getter(sc.hSimConnect, ctypes.byref(out))
        return out.value or None
    except Exception:
        return None


def probe_caps(sc) -> dict[str, bool]:
    """探测 DLL 到底封装了哪些目标函数。这是最安全、最先该跑的一步。"""
    caps = {}
    print("── DLL 能力探测 ──")
    for name in TARGET_FUNCS:
        ok = hasattr(sc.dll, name)
        caps[name] = ok
        print(f"  {'✓' if ok else '✗'} {name}")
    return caps


SIMCONNECT_UNUSED = 0xFFFFFFFF   # datumID 等"未使用"占位（库 Constants 同值）


def _add_datum(sc, def_id, name, unit, datatype):
    """AddToDataDefinition 薄封装。

    P0 probe 教训：最后一参 datumID 必须传 SIMCONNECT_UNUSED，传 0 等于
    把多个 datum 都挂到 client data 0 上 → 每个 DUPLICATE_ID，整个 data
    definition 直接废掉。
    """
    sc.dll.AddToDataDefinition(
        sc.hSimConnect, def_id, name.encode("ascii"),
        (unit or "").encode("ascii"), datatype, 0, SIMCONNECT_UNUSED)


def _request_user_position(sc, proc):
    """最小 data def 请求用户机位置 + 自身 title：既验证数据通道可用，又给
    后面的创建尝试提供"现场坐标"和"本机验证过的容器名"备选。失败无副作用。"""
    try:
        from SimConnect.Enum import SIMCONNECT_DATATYPE
        def_id = sc.new_def_id()
        req_id = sc.new_request_id().value
        for name, unit, dt in (("PLANE LATITUDE", "degrees",
                                "float64"),
                               ("PLANE LONGITUDE", "degrees",
                                "float64"),
                               ("PLANE ALTITUDE", "feet",
                                "float64"),
                               ("TITLE", None, "string256")):
            _add_datum(sc, def_id.value, name, unit,
                       getattr(SIMCONNECT_DATATYPE,
                               f"SIMCONNECT_DATATYPE_{dt.upper()}"))
        # RequestDataOnSimObject(h, req, def, obj, period, flags, origin, interval, limit)
        sc.dll.RequestDataOnSimObject(
            sc.hSimConnect, req_id, def_id.value, 0,  # SIMCONNECT_OBJECT_ID_USER
            1,                                        # SIMCONNECT_PERIOD_ONCE
            0, 0, 0, 0)                               # flags/origin/interval/limit
        _pump(sc, proc, 1.5)
        pos = _USER_POS.get(req_id)
        title = _USER_TITLE.get(req_id)
        if pos:
            print(f"  用户机位置 lat={pos[0]:.5f} lon={pos[1]:.5f} alt={pos[2]:.0f}ft"
                  f"（数据通道 OK；创建点将改到附近）")
        if title:
            print(f"  用户机容器 title={title!r}（本机验证可用的容器名）")
        return pos, title
    except Exception as e:  # noqa: BLE001
        print(f"  用户上下文请求失败（不影响后续创建探测）：{e!r}")
        return None, None


def _cleanup(sc, proc, obj_id, req):
    try:
        sc.dll.AIRemoveObject(sc.hSimConnect, obj_id, req)
        _pump(sc, proc, 1.0)
    except Exception as e:  # noqa: BLE001
        print(f"    清理失败：{e!r}")


def probe(sc, proc):
    """诊断矩阵：3 个创建 API × 若干机模 title。

    背景：spawn 返回 E_FAIL 且无 EXCEPTION，无法区分
    （a）主菜单未进世界 （b）title 不在装机列表 （c）创建 API 不可用。
    本模式对每种组合报 HRESULT 并等 ASSIGNED/EXCEPTION，成功的立即清理。
    """
    print("── 创建 API × 机模 title 探测矩阵（v2）──")
    print("  title 候选含：用户机涂装名（TITLE simvar）、可能的容器名变体、")
    print("  各 Asobo 默认机。NonATC 会分别尝试地面(OnGround=1)与空中放置。")

    # title 候选：用户 TITLE simvar 值 + 常见容器名变体 + Asobo 默认机。
    # 注意 TITLE simvar 返回的可能是**涂装名**（如 A350-900 (Default Cabin)），
    # 而 AICreateNonATCAircraft 要的是**容器名**（如 Airbus A350-900 Asobo），
    # 两者不一定相同——所以每个来源都试，包括其 Asobo 后缀/前缀变体。
    titles = [
        b"",                                # 空 = 默认 AI 机
        b"Airbus A320 Neo Asobo",           # 2020 默认 A320neo（历史默认值）
        b"Airbus A320neo Asobo",            # 大小写变体
        b"Asobo_A320_NEO",                  # 内部容器名形态
        b"Boeing 737-800 Asobo",
        b"Boeing 747-8f Asobo",
        b"Cessna 172 Skyhawk Asobo",
        b"Cessna 152 Asobo",
        b"Airbus A350-900 Asobo",           # 用户所驾机型可能的容器名
    ]
    env_title = os.environ.get("OF_TITLE")
    if env_title and env_title.encode("ascii", "ignore") not in titles:
        titles.insert(0, env_title.encode("ascii", "ignore"))
    airport = os.environ.get("OF_AIRPORT", "KJFK").encode("ascii", "ignore")

    _pos, _user_title = _request_user_position(sc, proc)
    if _user_title and _user_title.encode("ascii", "ignore") not in titles:
        titles.insert(0, _user_title.encode("ascii", "ignore"))
    # 用户机型容器名猜测："X (涂装名)" → "X Asobo" 形态试一个
    if _user_title and "(" in _user_title:
        base = _user_title.split("(", 1)[0].strip()
        for guess in (f"{base} Asobo", f"Airbus {base} Asobo", base):
            g = guess.encode("ascii", "ignore")
            if g and g not in titles:
                titles.append(g)
    # 创建点默认改到用户所在位置附近（P0 probe 教训：远离现实气泡必失败）
    initpos = _init_position(_pos)
    if _pos:
        print(f"  创建点 lat={initpos.Latitude:.5f} lon={initpos.Longitude:.5f}"
              f"（用户附近；OF_LAT/OF_LON 可覆盖）")
    if not os.environ.get("OF_AIRPORT"):
        print("  提示：ParkedATC 用机场由 OF_AIRPORT 指定（默认 KJFK，"
              "多半不在你的气泡内）；填你当前机场的 ICAO 再试那一行。")
    print(f"  title 候选 {len(titles)} 个；每个 {round(len(titles)*2.5/60,1)} 分钟"
          f"（NonATC 地面+空中 / ParkedATC / SimObj 四轮）……")

    req = 9600
    results = []

    for title in titles:
        label = title.decode("ascii") or "(空title=默认AI机)"
        # 1) AICreateNonATCAircraft——地面放置（OnGround=1）
        req += 1
        try:
            hr = sc.dll.AICreateNonATCAircraft(
                sc.hSimConnect, title, b"OFDIAG", initpos, req)
            packet = _last_packet_id(sc)
            _pump(sc, proc, _PROBE_PUMP_SECONDS)
            obj = _ASSIGNED.get(req)
            exc = next((e for e in _EXCEPTIONS
                        if e[1] in (packet, req)), None)
            print(f"  NonATCAircraft  title={label:<40} HRESULT={hr} "
                  f"obj={obj} exc={exc}")
            results.append(("NonATC", title, hr, obj, exc))
            if obj:
                _cleanup(sc, proc, obj, req)
        except Exception as e:  # noqa: BLE001
            print(f"  NonATCAircraft  title={label:<40} 抛异常 {e!r}")
            results.append(("NonATC", title, "EXC", None, None))

        # 2) AICreateParkedATCAircraft（按机场停机位创建）
        req += 1
        try:
            hr = sc.dll.AICreateParkedATCAircraft(
                sc.hSimConnect, title, b"OFDIAG", airport, req)
            packet = _last_packet_id(sc)
            _pump(sc, proc, _PROBE_PUMP_SECONDS)
            obj = _ASSIGNED.get(req)
            exc = next((e for e in _EXCEPTIONS
                        if e[1] in (packet, req)), None)
            print(f"  ParkedATC      title={label:<40} HRESULT={hr} "
                  f"obj={obj} exc={exc}")
            results.append(("ParkedATC", title, hr, obj, exc))
            if obj:
                _cleanup(sc, proc, obj, req)
        except Exception as e:  # noqa: BLE001
            print(f"  ParkedATC      title={label:<40} 抛异常 {e!r}")
            results.append(("ParkedATC", title, "EXC", None, None))

        # 3) AICreateSimulatedObject（非飞机模型备选）
        req += 1
        try:
            hr = sc.dll.AICreateSimulatedObject(
                sc.hSimConnect, title, initpos, req)
            packet = _last_packet_id(sc)
            _pump(sc, proc, _PROBE_PUMP_SECONDS)
            obj = _ASSIGNED.get(req)
            exc = next((e for e in _EXCEPTIONS
                        if e[1] in (packet, req)), None)
            print(f"  SimulatedObjs  title={label:<40} HRESULT={hr} "
                  f"obj={obj} exc={exc}")
            results.append(("SimObj", title, hr, obj, exc))
            if obj:
                _cleanup(sc, proc, obj, req)
        except Exception as e:  # noqa: BLE001
            print(f"  SimulatedObjs  title={label:<40} 抛异常 {e!r}")
            results.append(("SimObj", title, "EXC", None, None))

    # 4) 追加轮：NonATC 空中放置（OnGround=0，用户 altitude+1500ft）。
    #    若地面放置全 22 而空中可以 → 地面贴合/停机位约束问题，
    #    注入器可改为空中生成后按需对接待机位。
    if _pos:
        air_cls = _initpos_class()
        air_initpos = air_cls(
            Latitude=float(os.environ.get("OF_LAT", _pos[0] + 0.03)),
            Longitude=float(os.environ.get("OF_LON", _pos[1])),
            Altitude=float(os.environ.get("OF_ALT_FT", _pos[2] + 1500)),
            Pitch=0.0, Bank=0.0,
            Heading=float(os.environ.get("OF_HDG", "310")),
            OnGround=0, Airspeed=150)
        print("  ── 追加轮：NonATC 空中放置（OnGround=0）──")
        for title in titles:
            label = title.decode("ascii") or "(空title=默认AI机)"
            req += 1
            try:
                hr = sc.dll.AICreateNonATCAircraft(
                    sc.hSimConnect, title, b"OFDIAG", air_initpos, req)
                packet = _last_packet_id(sc)
                _pump(sc, proc, _PROBE_PUMP_SECONDS)
                obj = _ASSIGNED.get(req)
                exc = next((e for e in _EXCEPTIONS
                            if e[1] in (packet, req)), None)
                print(f"  NonATC(air)   title={label:<40} HRESULT={hr} "
                      f"obj={obj} exc={exc}")
                results.append(("NonATC-air", title, hr, obj, exc))
                if obj:
                    _cleanup(sc, proc, obj, req)
            except Exception as e:  # noqa: BLE001
                print(f"  NonATC(air)   title={label:<40} 抛异常 {e!r}")
                results.append(("NonATC-air", title, "EXC", None, None))

    print("── 汇总 ──")
    ok = [r for r in results if r[3] is not None]
    if ok:
        for api, title, _hr, obj, _exc in ok:
            print(f"  ✓ {api} + {title.decode('ascii') or '(默认AI机)'} → obj={obj}")
        print("  结论：该组合可用，P1 注入器按此调整 title/API。")
    else:
        hrs = {r[2] for r in results}
        excs = {r[4] for r in results if r[4]}
        print(f"  全部失败。HRESULT 集合={hrs}；EXCEPTION 集合={excs or '无回包'}。")
        if 33 in excs:
            print("  有 OBJECT_OUTSIDE_REALITY_BUBBLE(33) → 创建点/机场不在你")
            print("  的已加载区域内。创建点应默认取用户机附近（本脚本已这样做，")
            print("  若仍报 33，检查 OF_LAT/OF_LON 是否被设成了远方坐标）；")
            print("  ParkedATC 那行还要把 OF_AIRPORT 设成你当前机场的 ICAO。")
        elif hrs == {-2147467259} and not excs:
            print("  全部同步 E_FAIL 且零回包 → 基本坐实：MSFS 不在世界中"
                  "（主菜单/加载中），创建 API 不可用。请加载进一个机场再跑。")
        elif excs:
            print("  有 EXCEPTION 回包 → 按码表逐项定位（22=创建失败/")
            print("  34=title 不在装机列表 等）。")


def spawn(sc, proc, title=None, tail="OF001", req_id=9901, user_pos=None,
          user_title=None):
    """AICreateNonATCAircraft → 返回 objectID 或 None。[需实机验证]

    user_pos：用户机位置（probe/main 会先取）。创建点默认改到用户附近——
    P0 probe 教训：远离现实气泡必 CREATE_OBJECT_FAILED。
    user_title：用户当前飞机容器 title。OF_TITLE 未设时优先用它
    （P0-7/8：MSFS2024 上多数 Asobo 默认机容器不存在，用户机 title
    几乎总是可用），与 OwnedTrafficInjector 的兜底策略一致。
    """
    if not hasattr(sc.dll, "AICreateNonATCAircraft"):
        print("✗ 该 DLL 没有 AICreateNonATCAircraft —— A/B 两条路都要另找 API。")
        return None
    title = (title or os.environ.get("OF_TITLE") or user_title
             or "Airbus A320 Neo Asobo")
    title = title.encode("ascii", "ignore")
    if not os.environ.get("OF_TITLE") and user_title:
        print(f"  OF_TITLE 未设置 → 使用用户当前飞机 title：{user_title!r}")
    initpos = _init_position(user_pos)
    print(f"AICreateNonATCAircraft title={title!r} tail={tail} req={req_id} …")
    if user_pos:
        offset_nm = float(os.environ.get("OF_OFFSET_NM", "1.8"))
        print(f"  创建点 lat={initpos.Latitude:.5f} lon={initpos.Longitude:.5f}"
              f"（在你**正北 {offset_nm} 海里**，航向 {initpos.Heading:.0f}°，"
              f"贴地 {initpos.Altitude:.0f}ft）")
        print(f"  ★ 看到 objectID 后：切到外部/无人机视角，朝北看——"
              f"一架 {title.decode('ascii')} 会停在那里")
    try:
        hr = sc.dll.AICreateNonATCAircraft(
            sc.hSimConnect, title, tail.encode("ascii"), initpos, req_id)
        print(f"  返回 HRESULT={hr}")
        if hr != 0:
            print("  HRESULT!=S_OK——常见：主菜单未进世界 / title 不在装机列表。")
            print("  跑 probe 模式做组合探测；或先加载进机场再试。")
    except Exception as e:  # noqa: BLE001
        print(f"  调用抛异常：{e!r}  ←— 大概率是签名/结构体对齐不符，记进 §9")
        return None
    _pump(sc, proc, 5.0)                      # 等 ASSIGNED_OBJECT_ID 回包
    obj = _ASSIGNED.get(req_id)
    if obj is None:
        print("  → objectID = None")
        print("  常见原因（P0 实测排序）：1) 主菜单未进世界；")
        print("  2) title 不在装机列表——MSFS2024 常没有 'Airbus A320 Neo "
              "Asobo'；")
        print("     用 `set OF_TITLE=<你当前飞机的 TITLE simvar 值>` 重试"
              "（spawn 模式会自动打印它）；")
        print("  3) 创建点不在现实气泡内（spawn 已默认取用户附近）。")
        return None
    print(f"  → objectID = {obj}")
    return obj


def spawn_parked_atc(sc, proc, airport=None, title=None, tail="OF001",
                     req_id=9905, user_title=None):
    """AICreateParkedATCAircraft → objectID 或 None。

    **A 路径的正确打开方式**（P0-10 实测结论）：AICreateNonATCAircraft
    造的飞机按定义不受内建 ATC 管辖——给它飞行计划也原地不动。要让
    内建 AI-ATC 自己滑行/起飞，必须用 ParkedATC（在机场停机位创建、
    ATC 管辖）或 EnrouteATCAircraft。
    airport：ICAO（OF_AIRPORT 环境变量）。停机场按当前所在机场填。
    """
    if not hasattr(sc.dll, "AICreateParkedATCAircraft"):
        print("✗ 该 DLL 没有 AICreateParkedATCAircraft。")
        return None
    airport = (airport or os.environ.get("OF_AIRPORT") or "KJFK")
    title = ((title or os.environ.get("OF_TITLE") or user_title
              or "Airbus A320 Neo Asobo")).encode("ascii", "ignore")
    print(f"AICreateParkedATCAircraft airport={airport!r} title={title!r} "
          f"tail={tail} req={req_id} …")
    try:
        hr = sc.dll.AICreateParkedATCAircraft(
            sc.hSimConnect, title, tail.encode("ascii"),
            airport.encode("ascii"), req_id)
        packet = _last_packet_id(sc)
        print(f"  返回 HRESULT={hr}")
    except Exception as e:  # noqa: BLE001
        print(f"  调用抛异常：{e!r}")
        return None
    _pump(sc, proc, 5.0)
    obj = _ASSIGNED.get(req_id)
    if obj is None:
        exc = next((e for e in _EXCEPTIONS
                    if e[1] in (packet, req_id)), None)
        print(f"  → objectID = None（exc={exc}）。常见：机场 ICAO 不对/太远"
              f"（现实气泡外）/title 无效。")
        return None
    print(f"  → objectID = {obj}（已在该机场停机位，ATC 管辖中）")
    return obj


def _probe_object_position(sc, proc, obj_id, req_id):
    """对指定 object 请求 LAT/LON/ALT（PERIOD_ONCE），返回 (lat,lon,alt) 或 None。"""
    try:
        from SimConnect.Enum import SIMCONNECT_DATATYPE
        def_id = sc.new_def_id()
        for name in ("PLANE LATITUDE", "PLANE LONGITUDE", "PLANE ALTITUDE"):
            _add_datum(sc, def_id.value, name,
                       "feet" if name.endswith("ALTITUDE") else "degrees",
                       getattr(SIMCONNECT_DATATYPE,
                               "SIMCONNECT_DATATYPE_FLOAT64"))
        sc.dll.RequestDataOnSimObject(
            sc.hSimConnect, req_id, def_id.value, obj_id,
            1,                                        # SIMCONNECT_PERIOD_ONCE
            0, 0, 0, 0)
        _pump(sc, proc, 0.8)
        return _USER_POS.get(req_id)
    except Exception as e:  # noqa: BLE001
        print(f"  [diag] 位置探测失败：{e!r}")
        return None


def set_plan(sc, proc, obj_id, pln_path, req_id=9902):
    """AISetAircraftFlightPlan（A 路径：交给内建 AI-ATC 自主滑行起飞）。

    P0-11 诊断增强：MSFS 的 AI 离场是**排班制**的——停机位飞机挂上计划
    后要等放行调度/推出/开车/申请滑行，60 秒大概率不够。因此：
    观察窗默认 300s（OF_WATCH_SECONDS 可调），且每 30s 主动读该机的
    LAT/LON/ALT——**以位置数据判断它是否动过**，不依赖目视。
    """
    if not hasattr(sc.dll, "AISetAircraftFlightPlan"):
        print("✗ 无 AISetAircraftFlightPlan —— A 路径不可行，转 B。")
        return
    # SDK 要求路径不带 .PLN 扩展名（[需实机验证]）
    path = pln_path[:-4] if pln_path.lower().endswith(".pln") else pln_path
    print(f"AISetAircraftFlightPlan obj={obj_id} path={path!r} …")
    # 廉价前置校验：文件存在且是 XML（MSFS 的 .pln 是 XML）
    try:
        with open(pln_path, "rb") as fh:
            head = fh.read(64)
        if head.lstrip()[:5] != b"<?xml":
            print(f"  ⚠ {pln_path} 不像 MSFS 计划文件（XML）——"
                  f"用 MSFS 世界地图规划并保存的 .pln 更稳")
        else:
            print(f"  .pln 校验 OK（XML 计划文件，{os.path.getsize(pln_path)}B）")
    except OSError as e:
        print(f"  ⚠ 打不开 {pln_path}：{e}——路径不对计划定然加载不上")
    try:
        hr = sc.dll.AISetAircraftFlightPlan(
            sc.hSimConnect, obj_id, path.encode("ascii"), req_id)
        print(f"  返回 HRESULT={hr}")
    except Exception as e:  # noqa: BLE001
        print(f"  调用抛异常：{e!r}")
        return

    watch = float(os.environ.get("OF_WATCH_SECONDS", "300"))
    print(f"  观察 {watch:.0f}s（AI 离场是排班制的，可能要等几分钟）；")
    print("  每 30s 主动读该机 LAT/LON/ALT 判断它是否动过。")
    deadline = time.time() + watch
    probe_req = 9700
    base = _probe_object_position(sc, proc, obj_id, probe_req)
    if base:
        print(f"  初始位置 lat={base[0]:.5f} lon={base[1]:.5f} "
              f"alt={base[2]:.0f}ft")
    next_probe = time.time() + 30.0
    while time.time() < deadline:
        _pump(sc, proc, 1.0)
        if time.time() < next_probe:
            continue
        next_probe = time.time() + 30.0
        probe_req += 1
        pos = _probe_object_position(sc, proc, obj_id, probe_req)
        remaining = deadline - time.time()
        if pos is None:
            print(f"  [{watch - remaining:.0f}s] 位置探测无回包（剩余 "
                  f"{remaining:.0f}s）")
            continue
        if base is None:
            base = pos
        d_nm = ((pos[0] - base[0]) ** 2 + (pos[1] - base[1]) ** 2) ** 0.5 * 60.0
        print(f"  [{watch - remaining:.0f}s] lat={pos[0]:.5f} "
              f"lon={pos[1]:.5f} alt={pos[2]:.0f}ft"
              + (f" —— 已移动 {d_nm:.2f}NM" if d_nm > 0.01 else "（未动）")
              + f"，剩余 {remaining:.0f}s")
    if base:
        pos = _probe_object_position(sc, proc, obj_id, probe_req + 1)
        if pos:
            d_nm = ((pos[0] - base[0]) ** 2 + (pos[1] - base[1]) ** 2) ** 0.5 * 60.0
            print(f"  终态：累计移动 {d_nm:.2f}NM"
                  + ("；✅ 飞机动过（A 路径可行，目视确认动作）"
                     if d_nm > 0.01 else "；❌ 全程未动（A 路径在该构造下不可行）"))


def release_and_drive(sc, proc, obj_id, user_pos=None):
    """B 路径：AIReleaseControl 后逐帧 SetDataOnSimObject 推位置/航向，
    验证"写入是否即时生效"（不含真正运动模型，那是 P3-B 的活）。

    P0-8 教训：**不要写 GROUND VELOCITY**——它是计算类 simvar，不可写，
    SetDataOnSimObject 会回 DATA_ERROR(20)。逐帧驱动要写可写 simvar：
    PLANE LATITUDE / PLANE LONGITUDE / PLANE ALTITUDE /
    PLANE HEADING DEGREES TRUE（浮点 4 连发，32 字节一个单元）。
    [需实机验证] 整个链路。
    """
    if not hasattr(sc.dll, "AIReleaseControl"):
        print("✗ 无 AIReleaseControl —— B 路径不可行。")
        return
    print(f"AIReleaseControl obj={obj_id} …")
    try:
        hr = sc.dll.AIReleaseControl(sc.hSimConnect, obj_id, 9903)
        print(f"  返回 HRESULT={hr}")
    except Exception as e:  # noqa: BLE001
        print(f"  AIReleaseControl 抛异常：{e!r}")
        return

    if not hasattr(sc.dll, "SetDataOnSimObject"):
        print("✗ 无 SetDataOnSimObject —— 无法自驱动。")
        return
    # data def：位置 + 航向（可写 simvar；不要写 GROUND VELOCITY——P0-8）
    try:
        from SimConnect.Enum import SIMCONNECT_DATATYPE
        def_id = sc.new_def_id()
        for name, unit in (("PLANE LATITUDE", "degrees"),
                           ("PLANE LONGITUDE", "degrees"),
                           ("PLANE ALTITUDE", "feet"),
                           ("PLANE HEADING DEGREES TRUE", "degrees")):
            _add_datum(sc, def_id.value, name, unit,
                       getattr(SIMCONNECT_DATATYPE,
                               "SIMCONNECT_DATATYPE_FLOAT64"))
    except Exception as e:  # noqa: BLE001
        print(f"  AddToDataDefinition 失败：{e!r}")
        return

    base = user_pos or (51.47938, -0.46414, 95.0)
    # P0-11：1Hz 低频写入会被 sim 的停机/物理恢复拉回（实测）。B 路径的
    # 真实形态是**高频全状态写入**（计划 §4 P3-B 原话"每帧写"）——
    # OF_STEP_HZ 默认 10，OF_DRIVE_SECONDS 默认 10s，OF_DRIVE_NM 累计里程。
    hz = max(1, int(float(os.environ.get("OF_STEP_HZ", "10"))))
    seconds = float(os.environ.get("OF_DRIVE_SECONDS", "10"))
    total_nm = float(os.environ.get("OF_DRIVE_NM", "0.8"))
    steps = max(1, int(hz * seconds))
    step_nm = total_nm / steps
    print(f"  高频驱动：{steps} 步 × {hz}Hz（{seconds:.0f}s），沿 310° 累计 "
          f"{total_nm}NM，每步写 LAT/LON/ALT/HDG")
    print("  （1Hz 旧版实测会被拉回原地；这次看 10Hz 能否守住终点）")
    import math
    ok_steps = 0
    for i in range(steps + 1):
        dist_nm = i * step_nm
        dlat = dist_nm * math.cos(math.radians(310)) / 60.0
        dlon = dist_nm * math.sin(math.radians(310)) / 60.0 \
            / max(math.cos(math.radians(base[0])), 0.01)
        block = (ctypes.c_double * 4)(
            base[0] + dlat, base[1] + dlon, base[2], 310.0)
        try:
            hr = sc.dll.SetDataOnSimObject(
                sc.hSimConnect, def_id.value, obj_id,
                0, 0, ctypes.sizeof(block), block)
            if hr == 0:
                ok_steps += 1
        except Exception as e:  # noqa: BLE001
            print(f"    step {i}: SetDataOnSimObject 抛异常：{e!r}")
            break
        if i % max(1, steps // 5) == 0:
            print(f"    step {i}/{steps}: +{dist_nm:.2f}NM → HRESULT={hr}")
        time.sleep(1.0 / hz)
    print(f"  写完 {ok_steps}/{steps + 1} 步。现在读回该机实际位置验证终点…")
    _pump(sc, proc, 1.0)
    final = None
    try:
        final_req = 9800
        from SimConnect.Enum import SIMCONNECT_DATATYPE
        def_id2 = sc.new_def_id()
        for name in ("PLANE LATITUDE", "PLANE LONGITUDE", "PLANE ALTITUDE"):
            _add_datum(sc, def_id2.value, name,
                       "feet" if name.endswith("ALTITUDE") else "degrees",
                       getattr(SIMCONNECT_DATATYPE,
                               "SIMCONNECT_DATATYPE_FLOAT64"))
        sc.dll.RequestDataOnSimObject(
            sc.hSimConnect, final_req, def_id2.value, obj_id, 1, 0, 0, 0, 0)
        _pump(sc, proc, 1.5)
        final = _USER_POS.get(final_req)
    except Exception as e:  # noqa: BLE001
        print(f"  [diag] 读回失败：{e!r}")
    if final and base:
        d_nm = math.hypot(final[0] - base[0],
                          (final[1] - base[1]) * math.cos(math.radians(base[0]))) * 60.0
        print(f"  读回：lat={final[0]:.5f} lon={final[1]:.5f}"
              f"（相对起点 {d_nm:.2f}NM）")
        if d_nm > total_nm * 0.5:
            print("  ✅ 终点守住了 → B 路径（高频全状态写入）可行，P3-B 有戏")
        else:
            print("  ❌ 又被拉回 → sim 仍在恢复；下一步要连速度/姿态一起写")
    else:
        print("  （未读到回包，凭目视判断：飞机是否停在终点）")


def remove(sc, obj_id, req_id=9904):
    if obj_id is None or not hasattr(sc.dll, "AIRemoveObject"):
        return
    print(f"AIRemoveObject obj={obj_id} …")
    try:
        sc.dll.AIRemoveObject(sc.hSimConnect, obj_id, req_id)
    except Exception as e:  # noqa: BLE001
        print(f"  AIRemoveObject 抛异常：{e!r}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "caps"
    sc = connect()
    proc = _make_dispatch_proc(sc)
    _install_dispatch_chain(sc)
    try:
        caps = probe_caps(sc)
        if mode == "caps":
            return
        if mode == "probe":
            probe(sc, proc)
            return
        if not caps.get("AICreateNonATCAircraft"):
            print("没有创建 AI 的 DLL 入口，后续步骤无意义。停。")
            return

        # 先取用户机位置（创建点/兜底 title 的来源）
        user_pos, user_title = _request_user_position(sc, proc)

        if mode == "plan":
            # A 路径：ParkedATC 在机场创建（ATC 管辖）+ 飞行计划。
            # P0-10 实测：NonATC + 计划 = 原地不动（按定义不受 ATC 管辖）。
            pln = sys.argv[2] if len(sys.argv) > 2 else None
            if not pln:
                print("plan 模式需要一个 .PLN 路径参数。")
                return
            airport = os.environ.get("OF_AIRPORT")
            if not airport:
                print("  提示：先 set OF_AIRPORT=<你当前机场 ICAO，如 EGLL>")
                print("        （ParkedATC 按机场创建；计划也应是该机场出发的）。")
            obj = spawn_parked_atc(sc, proc, airport=airport,
                                   user_title=user_title)
            if obj is None:
                return
            set_plan(sc, proc, obj, pln)
            remove(sc, obj)
            _pump(sc, proc, 2.0)
            return

        if mode == "plan_nonatc":
            # 旧流程留档：NonATC + 计划（P0-10 已证不动，仅作对照）
            pln = sys.argv[2] if len(sys.argv) > 2 else None
            obj = spawn(sc, proc, user_pos=user_pos, user_title=user_title)
            if obj is None or not pln:
                return
            set_plan(sc, proc, obj, pln)
            remove(sc, obj)
            _pump(sc, proc, 2.0)
            return

        obj = spawn(sc, proc, user_pos=user_pos, user_title=user_title)
        if obj is None:
            print("未拿到 objectID，无法继续。检查上面的 HRESULT/EXCEPTION 记进 §9。")
            return

        if mode == "drive":
            release_and_drive(sc, proc, obj, user_pos)
        else:  # spawn：只创建观察
            watch = float(os.environ.get("OF_WATCH_SECONDS", "60"))
            print(f"已创建，{watch:.0f}s 后移除。现在去 MSFS 里确认：")
            print("  1) 切外部视角或无人机视角（座舱里 1.8 海里外很难注意到）；")
            print("  2) 机头朝向创建点方向（脚本上面打印的'正北 X 海里'）；")
            print("  3) 若仍看不到：检查 MSFS 选项→图形→交通→")
            print("     'AI 飞机密度'是否为 0（0=AI 飞机不渲染，但对象存在，")
            print("     这也是 FSLTL 交通会不会显示的总开关）。")
            print("  4) 想让它出现在更近处（550m）：set OF_OFFSET_NM=0.3 再跑。")
            deadline = time.time() + watch
            remaining = watch
            while remaining > 0:
                _pump(sc, proc, min(5.0, remaining))   # 持续收包不中断
                remaining = deadline - time.time()
                if remaining > 0:
                    print(f"  …观察中，剩余 {remaining:.0f}s（切视角找那架飞机）")
            print("  观察时间到，清理。")

        remove(sc, obj)
        _pump(sc, proc, _PROBE_PUMP_SECONDS)
    finally:
        try:
            sc.exit()
        except Exception:
            pass
        print("── spike 结束。请把实测结果誊抄进 docs/hybrid-owned-traffic-plan.md §9 ──")
        if _EXCEPTIONS:
            print(f"   本次收到的 SIMCONNECT_EXCEPTION：{_EXCEPTIONS}")


if __name__ == "__main__":
    main()
