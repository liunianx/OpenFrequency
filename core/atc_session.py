"""
atc_session.py — ATC 联络顺序状态机 + 跨管制员共享状态

这个模块是整个 ATC 对话的唯一事实来源 (single source of truth)：

1. 联络顺序：起飞必须从签派/放行 (Clearance Delivery) 开始，然后地面、塔台、离场……
   每个阶段只允许该阶段的管制员做该阶段的事，越权请求会被确定性重定向，
   不经过 LLM，因此不会出现"塔台发放行"这种顺序错误。
2. 跨管制员记忆：跑道、应答机、SID、巡航高度、QNH、滑行路线等由放行一次性指定，
   之后所有管制员只能读取，不能各自另编一个值。
3. 下一个联络人：每一步该联系谁、频率多少，由机场频率库确定性推导，
   带角色回退链（例如没有离场频率时用进近频率），永不返回 UNKNOWN。

状态存放在 shared_context["atc_state"]["session"]，因此 LLM prompt、
仪表盘、ATCMonitor 看到的永远是同一份数据。
"""

from __future__ import annotations

import hashlib
import re
import threading
import time

# ── 阶段梯 ───────────────────────────────────────────────────────────────────
# 起飞: 签派(PDC数据链) → ATIS → 放行 → 地面 → 塔台 → 离场 → 中心
# 降落: 中心 → 进近 → 塔台 → 地面 → 停机
# DISPATCH 只对 IFR 有意义（VFR 不申请预放行，初态直接是 ATIS，见 E12 修正）；
# DISPATCH 不参与调频反查（E13），只由 fpl_confirmed（PDC 复诵/WILCO）推进。
PHASES = [
    "DISPATCH", "ATIS", "CLEARANCE", "GROUND_DEP", "TOWER_DEP", "DEPARTURE",
    "CENTER", "APPROACH", "TOWER_ARR", "GROUND_ARR", "PARKED",
]

# 每个阶段对应的管制角色键（与 airport_frequency_service 的 role 一致）
PHASE_ROLE = {
    "DISPATCH": "Dispatch",
    "ATIS": "ATIS",
    "CLEARANCE": "Clearance Delivery",
    "GROUND_DEP": "Ground",
    "TOWER_DEP": "Tower",
    "DEPARTURE": "Departure",
    "CENTER": "Center",
    "APPROACH": "Approach",
    "TOWER_ARR": "Tower",
    "GROUND_ARR": "Ground",
    "PARKED": None,
}

PHASE_LABEL_ZH = {
    "DISPATCH": "签派", "ATIS": "通波", "CLEARANCE": "放行", "GROUND_DEP": "地面",
    "TOWER_DEP": "塔台", "DEPARTURE": "离场", "CENTER": "区域",
    "APPROACH": "进近", "TOWER_ARR": "塔台", "GROUND_ARR": "地面",
    "PARKED": "停机",
}

PHASE_LABEL_EN = {
    "DISPATCH": "Dispatch", "ATIS": "ATIS", "CLEARANCE": "Clearance Delivery",
    "GROUND_DEP": "Ground",
    "TOWER_DEP": "Tower", "DEPARTURE": "Departure", "CENTER": "Center",
    "APPROACH": "Approach", "TOWER_ARR": "Tower", "GROUND_ARR": "Ground",
    "PARKED": "Parked",
}

PHASE_LABEL_JA = {
    "DISPATCH": "ディスパッチ", "ATIS": "ATIS", "CLEARANCE": "クリアランス",
    "GROUND_DEP": "グランド",
    "TOWER_DEP": "タワー", "DEPARTURE": "デパーチャー", "CENTER": "センター",
    "APPROACH": "アプローチ", "TOWER_ARR": "タワー", "GROUND_ARR": "グランド",
    "PARKED": "駐機場",
}

ROLE_LABEL_ZH = {
    "ATIS": "通波", "Dispatch": "签派", "Clearance Delivery": "放行", "Ground": "地面",
    "Tower": "塔台", "Departure": "离场", "Center": "区域",
    "Approach": "进近", "Unicom": "Unicom", "Emergency": "紧急",
}

# 起飞一侧用起飞机场频率，降落一侧用目的地机场频率
_ARRIVAL_PHASES = {"APPROACH", "TOWER_ARR", "GROUND_ARR"}

# 每个角色查频率时的回退链：数据库缺该角色时依次尝试
# Dispatch 显示 CD 频率仅供 UI（PDC 实际走 ACARS/CPDLC 数据链，不占用语音频率，见 E13）
ROLE_FREQ_FALLBACKS = {
    "ATIS": ("ATIS",),
    "Dispatch": ("Clearance Delivery",),
    "Clearance Delivery": ("Clearance Delivery", "Ground"),
    "Ground": ("Ground",),
    "Tower": ("Tower",),
    "Departure": ("Departure", "Approach", "Center"),
    "Center": ("Center", "Approach"),
    "Approach": ("Approach", "Center"),
}

# 每个阶段允许下发的指令
PHASE_AUTHORITY = {
    "DISPATCH": {"pdc", "flightplan_confirm"},
    "ATIS": set(),
    "CLEARANCE": {"ifr_clearance"},
    "GROUND_DEP": {"pushback", "taxi", "runway_crossing"},
    "TOWER_DEP": {"lineup", "takeoff", "initial_climb"},
    "DEPARTURE": {"vectors", "climb", "descent"},
    "CENTER": {"vectors", "climb", "descent"},
    "APPROACH": {"vectors", "climb", "descent", "approach_clearance"},
    "TOWER_ARR": {"landing"},
    "GROUND_ARR": {"taxi", "gate_assignment", "taxi_to_gate"},
    "PARKED": set(),
}

# 意图 → 拥有该指令的阶段
ACTION_OWNER = {
    "pdc": "DISPATCH",
    "flightplan_confirm": "DISPATCH",
    "ifr_clearance": "CLEARANCE",
    "pushback": "GROUND_DEP",
    "taxi": "GROUND_DEP",
    "gate_request": "GROUND_ARR",
    "gate_assignment": "GROUND_ARR",
    "taxi_to_gate": "GROUND_ARR",
    "runway_crossing": "GROUND_DEP",
    "lineup": "TOWER_DEP",
    "takeoff": "TOWER_DEP",
    "initial_climb": "TOWER_DEP",
    "vectors": "DEPARTURE",
    "climb": "DEPARTURE",
    "descent": "DEPARTURE",
    "approach_clearance": "APPROACH",
    "landing": "TOWER_ARR",
}

# 某些指令要求前置条件已经成立（否则说明飞行员跳过了签派/没推出）。
# 值可以是字段名（str）或 callable(session)->bool：返回 True 表示前置未满足。
# callable 用于表达"站立机位可直接滑出时例外"（G2），静态 tuple 表达不了。
# 前置缺失时的重定向目标：默认 CLEARANCE；taxi 缺推出时target GROUND_DEP。
ACTION_PREREQS = {
    "lineup": ("runway",),
    "takeoff": ("runway", "squawk"),
    "landing": ("runway",),
    # pushback_done/pushback_ok 存在 state 里而不是 assigned，用 s.state 读
    "taxi": (lambda s: not (s.state.get("pushback_done") or s.state.get("pushback_ok")),),
}

# 前置条件缺失时应该把飞行员送去哪个阶段（不在此表中的一律回 CLEARANCE）
PREREQ_REDIRECT = {
    "taxi": "GROUND_DEP",
}

# 意图识别（中英双语）。顺序即优先级：PDC/停机位/推出完成必须排在泛化意图之前。
_INTENT_PATTERNS = [
    ("pdc", (r"申请.{0,4}预放行", r"请求.{0,4}预放行", r"predeparture clearance",
             r"request pdc", r"\bpdc\b", r"clearance on request")),
    ("flightplan_confirm", (r"复诵.{0,4}预放行", r"预放行.{0,4}(?:确认|复诵)",
                            r"w?ilco", r"flight ?plan (?:is )?confirmed")),
    ("gate_request", (r"申请停机位", r"请求停机位", r"分配停机位", r"申请廊桥",
                      r"request gate", r"gate assignment")),
    ("taxi_to_gate", (r"滑行到停机位", r"滑行至停机位", r"滑行到廊桥",
                      r"taxi to (?:the )?(?:gate|stand)")),
    ("pushback_complete", (r"推出完成", r"推出完毕", r"pushback complete")),
    ("engines_started", (r"启动完成", r"开车完成", r"发动机启动完成",
                         r"engines? (?:are )?started")),
    ("takeoff", (r"申请.{0,6}起飞", r"请求.{0,6}起飞", r"准备起飞", r"可以起飞", r"ready for (?:takeoff|departure)",
                 r"cleared for takeoff", r"request takeoff")),
    ("runway_request", (r"申请使用?跑道", r"请求使用?跑道", r"使用跑道", r"换跑道", r"改跑道",
                        r"request runway", r"runway \d")),
    ("ifr_clearance", (r"申请放行", r"请求放行", r"放行许可", r"申请(?:ifr)?许可",
                       r"request (?:ifr )?clearance", r"clearance delivery", r"request clearance")),
    ("pushback", (r"推出", r"开车", r"启动", r"request pushback", r"push ?back")),
    ("taxi", (r"申请滑行", r"请求滑行", r"滑行到", r"滑行至", r"request taxi", r"ready for taxi")),
    ("landing", (r"申请落地", r"申请着陆", r"请求落地", r"请求着陆", r"申请进近落地",
                 r"request landing", r"cleared to land", r"cleared for landing")),
    ("approach_clearance", (r"申请进近", r"进近许可", r"request (?:ils|rnav|rnp|visual)? ?approach",
                           r"approach clearance")),
    ("lineup", (r"进[入]?跑道", r"对准跑道", r"上跑道", r"line ?up", r"position and hold")),
    ("climb", (r"申请爬升", r"请求爬升", r"爬升至", r"上升至", r"request (?:higher|climb)",
               r"request climb")),
    ("descent", (r"申请下降", r"请求下降", r"下降至", r"request (?:lower|descent)", r"request descent")),
    ("vectors", (r"雷达引导", r"引导", r"radar vector", r"\bvectors?\b")),
]

_COMPILED_INTENTS = [(name, tuple(re.compile(p, re.I) for p in pats)) for name, pats in _INTENT_PATTERNS]

# 角色名识别（用于解析"联系离场 120.4"这类句子里的角色）
ROLE_TEXT_ALIASES = [
    ("Dispatch", ("签派", "dispatch")),
    ("Clearance Delivery", ("放行", "clearance delivery", "clearance", "delivery")),
    ("Ground", ("地面", "ground")),
    ("Tower", ("塔台", "tower")),
    ("Departure", ("离场", "departure")),
    ("Approach", ("进近", "approach")),
    ("Center", ("区调", "区域管制", "区域", "中心", "centre", "center", "radar")),
    ("ATIS", ("通波", "atis")),
]

# 频率问题
_FREQ_QUERY = re.compile(
    r"(?:频率|频点).{0,6}(?:是多少|是多少|是几|多少|怎么)|(?:多少|什么|哪个).{0,4}频率"
    r"|what(?:'s| is)? (?:the )?frequency|which frequency|frequency\?",
    re.I,
)

# 紧急/非管制频率，不参与阶段机
NON_ATC_ROLES = {"Unicom", "Emergency", "ATC"}

# 降落一侧阶段的起始下标（塔台/地面在梯子里各出现两次）
_ARRIVAL_START = PHASES.index("APPROACH")

# 一次性指定后不再变动的字段（first-write-wins），其余字段后者覆盖前者
# star: SID/STAR 一经放行不再变；approach_clearance 故意不在此列——进近许可会
# 合法变更（改跑道、复飞后重新指定），设 sticky 会让第二次进近许可被 assign 静默拒绝。
STICKY_FIELDS = {"callsign", "squawk", "runway", "arrival_runway", "sid", "star", "cruise_alt"}

_CN_NUM = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 保留/禁用应答机编码
_RESERVED_SQUAWKS = {"7500", "7600", "7700"}


def normalize_runway(value) -> str | None:
    """把各种写法的跑道号归一成可比较的键：'02R'/'2R'/'跑道二十右' → '2R'。"""
    if not value:
        return None
    text = str(value).strip().upper()
    text = re.sub(r"^(?:RWY?|RUNWAY|跑道)\s*", "", text)
    text = re.sub(r"\s*(?:RWY?|RUNWAY|跑道)$", "", text)
    m = re.match(r"^0*(\d{1,2})\s*([LRC])?$", text)
    if m:
        return f"{int(m.group(1))}{m.group(2) or ''}"
    m = re.match(r"^0*(\d{1,2})\s*([LRC])?$", text)
    if m:
        return f"{int(m.group(1))}{m.group(2) or ''}"
    # 中文数字：二十右 / 二十
    m = re.match(r"^([零一二三四五六七八九十]{1,3})\s*([左右中])?$", text)
    if m:
        digits = m.group(1)
        if digits == "十":
            num = 10
        elif "十" in digits:
            head, _, tail = digits.partition("十")
            num = (_CN_NUM.get(head, 1) * 10) + (_CN_NUM.get(tail, 0) if tail else 0)
        else:
            num = _CN_NUM.get(digits, 0)
        if num:
            suffix = {"左": "L", "右": "R", "中": "C"}.get(m.group(2) or "", "")
            return f"{num}{suffix}"
    return None


def runway_display(runway, style: str = "ascii") -> str:
    """把规范跑道号渲染成对应语言的写法。"""
    if not runway:
        return ""
    if style == "cn_digits":
        m = re.match(r"^(\d{1,2})([LRC]?)$", runway)
        if not m:
            return runway
        return f"{m.group(1)}{_CN_SUFFIX[m.group(2)]}"
    if style == "zh_num":
        m = re.match(r"^(\d{1,2})([LRC]?)$", runway)
        if not m:
            return runway
        num = int(m.group(1))
        if num < 10:
            digits = list(_CN_NUM.keys())[list(_CN_NUM.values()).index(num)]
        elif num == 10:
            digits = "十"
        elif num < 20:
            digits = "十" + (list(_CN_NUM.keys())[list(_CN_NUM.values()).index(num - 10)] if num > 10 else "")
        else:
            tens, ones = divmod(num, 10)
            digits = list(_CN_NUM.keys())[list(_CN_NUM.values()).index(tens)] + "十"
            if ones:
                digits += list(_CN_NUM.keys())[list(_CN_NUM.values()).index(ones)]
        suffix = {"L": "左", "R": "右", "C": "中"}.get(m.group(2), "")
        return f"{digits}{suffix}"
    return runway


def extract_runway(text) -> str | None:
    """从一句话里提取跑道号（支持中英文与中文数字）。"""
    if not text:
        return None
    patterns = [
        r"(?:跑道|runway|rwy)\s*0*(\d{1,2})\s*([LRC左右中])?",
        r"0*(\d{1,2})\s*([LRC左右中])?\s*跑道",
        r"(?:跑道)\s*([零一二三四五六七八九十]{1,3})\s*([左右中])?",
        r"([零一二三四五六七八九十]{1,3})\s*([左右中])?\s*跑道",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            digits, suffix = str(m.group(1)), m.group(2) or ""
            if digits.isdigit():
                return normalize_runway(digits + _SUFFIX_TO_ASCII.get(suffix, suffix))
            return normalize_runway(digits + suffix)
    return None


# 从管制通话原文里直接抽取权威字段（中英双语）
# InstructionExtractor 的卡片偏英文，中文"应答机4231"这类写法抽不出来，
# 而这里漏一次，后面所有管制员就都看不到这个值了。
_TEXT_FIELD_PATTERNS = [
    ("squawk", re.compile(r"(?:squawk|应答机)\s*[:：]?\s*(\d{4})", re.I)),
    ("sid", re.compile(r"(?:via|经|沿)\s*([A-Z]{2,6}\d[A-Z]?)", re.I)),
    ("cruise_alt", re.compile(
        r"(?:cruise(?:\s+altitude)?|巡航高度)\s*[:：]?\s*(FL\s?\d{2,3}|M\s?\d{3,5}|\d{4,5})", re.I)),
    ("altimeter", re.compile(
        r"(?:qnh|altimeter|修正海压|气压)\s*[:：]?\s*(\d{4})", re.I)),
    ("cleared_altitude", re.compile(
        r"(?:climb|descend)(?:\s+and\s+maintain)?(?:\s+to)?\s+(FL\s?\d{2,3}|M\s?\d{3,5}|\d{3,5})"
        r"|(?:上升至|爬升至|下降至|下降到|保持)\s*(FL\s?\d{2,3}|M\s?\d{3,5}|\d{3,5})",
        re.I)),
    ("assigned_heading", re.compile(
        r"(?:fly\s+heading|heading|飞航向|航向)\s*[:：]?\s*(\d{3})", re.I)),
    ("assigned_speed", re.compile(
        r"(?:(?:reduce|increase)\s+speed\s+to|减速至|加速至|速度)\s*[:：]?\s*(\d{2,3})\s*(?:节|kt|knots)?",
        re.I)),
]


def extract_clearance_values(text) -> dict:
    """从管制员的一句话里抽出可以写入权威状态的字段。"""
    if not text:
        return {}
    out = {}
    for field, pattern in _TEXT_FIELD_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        value = next((g for g in m.groups() if g), None)
        if value:
            out[field] = re.sub(r"\s+", "", str(value)).upper()
    return out


def squawk_for_callsign(callsign) -> str:
    """由呼号稳定派生应答机编码——所有管制员拿到的是同一个值。"""
    digest = hashlib.sha1((callsign or "Aircraft").encode("utf-8")).hexdigest()
    value = int(digest[:6], 16) % 4096
    squawk = f"{value:04o}"[-4:]
    if squawk in {"0000"} | _RESERVED_SQUAWKS:
        squawk = "1201"
    return squawk


def role_from_text(text) -> str | None:
    """从一句话里认出提到的管制角色。"""
    if not text:
        return None
    lower = str(text).lower()
    for role, aliases in ROLE_TEXT_ALIASES:
        if any(alias in lower for alias in aliases):
            return role
    return None


def detect_intent(text) -> str | None:
    """识别飞行员请求属于哪一类指令。"""
    if not text:
        return None
    for name, patterns in _COMPILED_INTENTS:
        if any(p.search(text) for p in patterns):
            return name
    return None


def _new_state(flight_rules: str = "IFR") -> dict:
    rules = "VFR" if str(flight_rules).upper() == "VFR" else "IFR"
    return {
        # E12: VFR/通航航班不申请 PDC，初态直接 ATIS，绝不卡在 DISPATCH
        "phase": "ATIS" if rules == "VFR" else "DISPATCH",
        "flight_rules": rules,
        "origin": None,
        "destination": None,
        "cruise_alt": 0,
        "atis_letter": None,
        "atis_copied": False,
        # PDC（预放行）复诵/WILCO 确认；DISPATCH→ATIS 只由它（或 VFR）触发（E12/E13）
        "fpl_confirmed": False,
        # 推出/开车状态（A2）；pushback_ok=True 表示站立机位可直接滑出，无需推出
        "pushback_done": False,
        "engines_started": False,
        "pushback_ok": False,
        "descending": False,
        "airborne": False,
        "tuned_role": None,
        # field -> {"value":..., "by":..., "ts":...}，first-write-wins
        "assigned": {},
        # 已完成的移交记录，避免重复下达同一条移交指令
        "handoffs": [],
        "pending_next": None,
    }


class ATCSession:
    """联络顺序 + 跨管制员共享状态。所有读写都加锁，可被多线程安全访问。"""

    # 会在 prompt 里展示的权威字段
    PROMPT_FIELDS = [
        ("squawk", "Squawk", "应答机"),
        ("runway", "Departure runway", "起飞跑道"),
        ("arrival_runway", "Landing runway", "落地跑道"),
        ("sid", "SID", "离场程序"),
        ("star", "STAR", "进场程序"),
        ("cruise_alt", "Cruise altitude", "巡航高度"),
        ("cleared_altitude", "Cleared altitude", "许可高度"),
        ("assigned_heading", "Assigned heading", "许可航向"),
        ("assigned_speed", "Assigned speed", "许可速度"),
        ("altimeter", "Altimeter", "修正海压"),
        ("taxi_route", "Taxi route", "滑行路线"),
        ("approach_clearance", "Approach", "进近方式"),
        ("assigned_gate", "Gate", "停机位"),
        ("hold_short_runway", "Hold short", "等待点"),
    ]

    def __init__(self, config=None, airport_frequency_service=None):
        self.config = config or {}
        self.freq_service = airport_frequency_service
        self._lock = threading.RLock()
        self._state = _new_state()

    # ── 装配 ────────────────────────────────────────────────────────────────

    def adopt(self, state: dict) -> None:
        """从 shared_context 里恢复状态（llm_client 每次请求都会重建 session 实例）。"""
        with self._lock:
            if isinstance(state, dict) and state:
                self._state.clear()
                self._state.update(state)
                # 兼容旧快照：缺新字段时补默认值
                self._state.setdefault("flight_rules", "IFR")
                self._state.setdefault("fpl_confirmed", False)
                self._state.setdefault("pushback_done", False)
                self._state.setdefault("engines_started", False)
                self._state.setdefault("pushback_ok", False)

    def attach(self, context: dict) -> None:
        """把状态挂到 shared_context 上，让所有模块看到同一份数据。"""
        with self._lock:
            atc_state = context.setdefault("atc_state", {})
            atc_state["session"] = self._state

    @property
    def state(self) -> dict:
        with self._lock:
            return self._state

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def reset(self, flight_plan=None, callsign=None) -> None:
        """新航班/新呼号时重置。"""
        with self._lock:
            rules = (flight_plan or {}).get("flight_rules") or self._state.get("flight_rules") or "IFR"
            self._state.clear()
            self._state.update(_new_state(rules))
            self.load_from_flight_plan(flight_plan or {}, callsign=callsign)

    def load_from_flight_plan(self, flight_plan: dict, callsign=None) -> None:
        fp = flight_plan or {}
        with self._lock:
            origin = (fp.get("origin") or "").strip().upper()
            dest = (fp.get("destination") or "").strip().upper()
            if origin and origin != "N/A":
                self._state["origin"] = origin
            if dest and dest != "N/A":
                self._state["destination"] = dest
            fp_rules = (fp.get("flight_rules") or "").upper()
            if fp_rules in ("IFR", "VFR"):
                self._state["flight_rules"] = fp_rules
                # E12：装载 VFR 计划时若还停在 DISPATCH，直接放行到 ATIS
                if fp_rules == "VFR" and self._state["phase"] == "DISPATCH":
                    self._state["phase"] = "ATIS"
            try:
                self._state["cruise_alt"] = int(fp.get("cruise_alt") or 0)
            except (TypeError, ValueError):
                self._state["cruise_alt"] = 0
            if callsign:
                self.assign("callsign", callsign, by="flight plan")

    def set_flight_rules(self, rules) -> str:
        """切换 IFR/VFR。VFR 下若还停在 DISPATCH，直接放行到 ATIS（E12 保险）。"""
        rules = "VFR" if str(rules).upper() == "VFR" else "IFR"
        with self._lock:
            self._state["flight_rules"] = rules
            if rules == "VFR" and self._state["phase"] == "DISPATCH":
                self._state["phase"] = "ATIS"
        return rules

    def mark_atis_copied(self, letter=None) -> None:
        with self._lock:
            self._state["atis_copied"] = True
            if letter:
                self._state["atis_letter"] = str(letter).upper()

    def confirm_flight_plan(self) -> None:
        """PDC 复诵 / 数据链 WILCO：预放行确认。推进由 observe_telemetry 完成。"""
        with self._lock:
            self._state["fpl_confirmed"] = True

    def mark_pushback_done(self) -> None:
        with self._lock:
            self._state["pushback_done"] = True

    def mark_engines_started(self) -> None:
        with self._lock:
            self._state["engines_started"] = True

    def set_pushback_ok(self, ok: bool) -> None:
        """站立机位可直接滑出（无需推出）时置 True，放开 taxi 前置条件（G2）。"""
        with self._lock:
            self._state["pushback_ok"] = bool(ok)

    # ── 阶段 ────────────────────────────────────────────────────────────────

    @property
    def phase(self) -> str:
        with self._lock:
            return self._state["phase"]

    @property
    def phase_index(self) -> int:
        return PHASES.index(self.phase)

    def phase_role(self, phase=None) -> str | None:
        return PHASE_ROLE[phase or self.phase]

    def advance_to(self, phase: str) -> bool:
        """只能向前推进。"""
        if phase not in PHASES:
            return False
        with self._lock:
            if PHASES.index(phase) <= PHASES.index(self._state["phase"]):
                return False
            self._state["phase"] = phase
            return True

    def advance_to_role(self, role_key: str) -> bool:
        """
        飞行员调到某个角色的频率时，把阶段对齐到该角色对应的阶段。
        - 前进：正常联络顺序；
        - 回退：说明之前跳步了（例如没放行就上塔台），允许回到前面的台补课；
        - 起飞阶段不会直接切到降落一侧的同名角色（塔台/地面各出现两次）。
        - DISPATCH 排除在频率反查之外（E13）：PDC 走数据链，与语音调频解耦，
          否则 tune 到 CD 频率会命中 DISPATCH 而非 CLEARANCE。
        """
        if not role_key or role_key in NON_ATC_ROLES:
            return False
        with self._lock:
            candidates = [idx for idx, name in enumerate(PHASES)
                          if PHASE_ROLE[name] == role_key and PHASE_ROLE[name] != "Dispatch"]
            if not candidates:
                return False
            current = PHASES.index(self._state["phase"])
            if current in candidates:
                return False
            forward = [idx for idx in candidates if idx > current]
            backward = [idx for idx in candidates if idx < current]
            target = None
            if forward:
                candidate = forward[0]
                if candidate >= _ARRIVAL_START and not self._state.get("descending"):
                    candidate = None      # 起飞时不该直接切到降落段的塔台/地面
                target = candidate
            if target is None and backward:
                target = backward[-1]
            if target is None:
                return False
            self._state["phase"] = PHASES[target]
            return True

    def observe_tuning(self, role_key) -> str | None:
        """
        飞行员调到某个角色的频率。按联络顺序对齐阶段，
        返回对齐后的阶段名；没有变化时返回 None。
        """
        if not role_key or role_key in NON_ATC_ROLES:
            return None
        with self._lock:
            self._state["tuned_role"] = role_key
        if self.advance_to_role(role_key):
            return self.phase
        return None

    def next_phase(self) -> str | None:
        with self._lock:
            idx = PHASES.index(self._state["phase"])
            if idx + 1 >= len(PHASES):
                return None
            nxt = PHASES[idx + 1]
            # 离场之后：在下降就去进近，否则去区域
            if self._state["phase"] == "DEPARTURE":
                if self._state["descending"]:
                    return "APPROACH"
                return "CENTER"
            return nxt

    def observe_telemetry(self, on_ground=None, altitude=None, vs=None, groundspeed=None) -> str | None:
        """按遥测自动推进阶段，返回新阶段名（没有推进则返回 None）。"""
        with self._lock:
            if vs is not None:
                self._state["descending"] = float(vs) < -200
            if on_ground is not None:
                self._state["airborne"] = (not bool(on_ground)) and float(altitude or 0) > 50
            phase = self._state["phase"]
            alt = float(altitude or 0)
            gs = float(groundspeed or 0)
            target = None
            if phase == "DISPATCH":
                # E12：VFR 分流优先于 PDC 确认；IFR 必须等 fpl_confirmed
                if self._state.get("flight_rules") == "VFR" or self._state.get("fpl_confirmed"):
                    target = "ATIS"
            elif phase == "ATIS" and self._state["atis_copied"]:
                target = "CLEARANCE"
            elif phase == "GROUND_DEP" and not on_ground:
                target = "TOWER_DEP"
            elif phase == "TOWER_DEP" and not on_ground and alt > 500:
                target = "DEPARTURE"
            elif phase == "DEPARTURE" and not on_ground and alt > 18000 and abs(float(vs or 0)) < 500:
                target = "CENTER"
            elif phase == "CENTER" and float(vs or 0) < -300 and alt < (self._state["cruise_alt"] or 99999) * 0.8:
                target = "APPROACH"
            elif phase == "APPROACH" and not on_ground and alt < 3000:
                target = "TOWER_ARR"
            elif phase == "TOWER_ARR" and on_ground and gs < 80:
                target = "GROUND_ARR"
            elif phase == "GROUND_ARR" and on_ground and gs < 1:
                target = "PARKED"
            if target and PHASES.index(target) > PHASES.index(phase):
                self._state["phase"] = target
                return target
            return None

    # ── 权威字段 ────────────────────────────────────────────────────────────

    def get(self, field):
        with self._lock:
            entry = self._state["assigned"].get(field)
            return entry.get("value") if entry else None

    def get_entry(self, field):
        with self._lock:
            return self._state["assigned"].get(field)

    def assign(self, field, value, by="ATC", force=False) -> bool:
        """
        写入权威字段。
        STICKY_FIELDS（应答机、跑道、SID、巡航高度、呼号）first-write-wins，
        后一个管制员无法悄悄改掉放行给的值；其余字段（高度/航向/速度/海压等）
        后者覆盖前者，因为它们在飞行中本来就会变。
        """
        if value in (None, "", [], {}):
            return False
        sticky = field in STICKY_FIELDS
        with self._lock:
            existing = self._state["assigned"].get(field)
            if existing and str(existing.get("value")) != str(value) and sticky and not force:
                return False
            self._state["assigned"][field] = {
                "value": value,
                "by": by,
                "ts": time.time(),
            }
            return True

    def change(self, field, value, by="ATC") -> bool:
        """飞行员明确要求更改时才允许覆盖权威字段。"""
        return self.assign(field, value, by=by, force=True)

    def change_runway(self, value, by="ATC", arrival=False):
        """飞行员明确申请更换跑道（起飞前由地面/塔台批准）。"""
        return self.propose_runway(value, by=by, arrival=arrival, allow_change=True)

    def _display_ident(self, airport_ident, canonical):
        """把规范化键换回机场数据里的原始写法（'2R' → '02R'）。"""
        if not airport_ident or not self.freq_service or not canonical:
            return None
        try:
            for ident in self.freq_service.get_airport_runways(airport_ident):
                if normalize_runway(ident) == canonical:
                    return ident
        except Exception:
            pass
        return None

    def propose_runway(self, value, by="ATC", arrival=False, allow_change=False):
        """
        申请跑道。返回 (ok, display, reason)。
        校验通过才写入，避免"20R"被写成"02R"这种反向/错误跑道。
        """
        canonical = normalize_runway(value)
        if not canonical:
            return False, None, "unparsable"
        field = "arrival_runway" if arrival else "runway"
        airport = self._airport_for_phase(arrival)
        candidates = self._airport_runways(airport)
        if candidates and canonical not in candidates:
            return False, canonical, "not_at_airport"
        display = self._display_ident(airport, canonical) or canonical
        with self._lock:
            existing = self._state["assigned"].get(field)
            if existing and normalize_runway(existing.get("value")) != canonical:
                if not allow_change:
                    return False, canonical, "already_assigned"
            self._state["assigned"][field] = {"value": display, "by": by, "ts": time.time()}
            return True, display, "ok"

    def active_runway(self, arrival=False):
        with self._lock:
            field = "arrival_runway" if arrival else "runway"
            entry = self._state["assigned"].get(field) or self._state["assigned"].get("runway")
            return entry.get("value") if entry else None

    def _airport_for_phase(self, arrival=False) -> str | None:
        with self._lock:
            return self._state["destination"] if arrival else (self._state["origin"] or self._state["destination"])

    def _airport_runways(self, airport_ident):
        if not airport_ident or not self.freq_service:
            return []
        try:
            raw = self.freq_service.get_airport_runways(airport_ident)
        except Exception:
            return []
        out = []
        for item in raw or []:
            canonical = normalize_runway(item)
            if canonical and canonical not in out:
                out.append(canonical)
        return out

    def issued_instructions(self) -> dict:
        """兼容旧消费者（quick_reply / 仪表盘）的扁平视图。"""
        with self._lock:
            return {field: entry["value"] for field, entry in self._state["assigned"].items()}

    # ── 频率 ────────────────────────────────────────────────────────────────

    def frequency_for(self, role_key, airport_ident=None) -> str | None:
        """按角色取频率，带回退链与 config 兜底，尽量不返回 None。"""
        if not role_key or role_key in NON_ATC_ROLES:
            return None
        airport = airport_ident or self._airport_for_phase(role_key in ("Approach",))
        candidates = ROLE_FREQ_FALLBACKS.get(role_key, (role_key,))
        if self.freq_service and airport:
            try:
                freq_map = self.freq_service.get_frequency_map(airport)
            except Exception:
                freq_map = {}
            for candidate in candidates:
                value = freq_map.get(candidate)
                if value:
                    return f"{float(value):.3f}"
        config_freqs = self.config.get("frequencies", {}) or {}
        for candidate in candidates:
            value = config_freqs.get(candidate)
            if value:
                try:
                    return f"{float(value):.3f}"
                except (TypeError, ValueError):
                    continue
        return None

    def contact_for(self, phase) -> dict | None:
        """某个阶段应该联系的管制员 + 频率。"""
        role = PHASE_ROLE.get(phase)
        if not role:
            return None
        arrival = phase in _ARRIVAL_PHASES
        airport = self._airport_for_phase(arrival)
        return {
            "phase": phase,
            "role": role,
            "role_zh": ROLE_LABEL_ZH.get(role, role),
            "frequency": self.frequency_for(role, airport),
            "airport": airport,
        }

    def sequence_for_ui(self) -> list:
        """给前端用的紧凑阶段列表。"""
        with self._lock:
            phase = self._state["phase"]
            current_idx = PHASES.index(phase)
        out = []
        for idx, name in enumerate(PHASES):
            role = PHASE_ROLE[name]
            if role is None:
                continue
            contact = self.contact_for(name)
            out.append({
                "phase": name,
                "label_zh": PHASE_LABEL_ZH[name],
                "label_en": PHASE_LABEL_EN[name],
                "label_ja": PHASE_LABEL_JA.get(name, PHASE_LABEL_EN[name]),
                "role": role,
                "frequency": contact["frequency"] if contact else None,
                "state": "done" if idx < current_idx else ("current" if idx == current_idx else "next"),
            })
        return out

    def next_contact(self) -> dict | None:
        with self._lock:
            nxt = self.next_phase()
            pending = self._state.get("pending_next")
        if pending and pending.get("frequency"):
            return pending
        return self.contact_for(nxt) if nxt else None

    def set_pending_next(self, contact) -> None:
        with self._lock:
            self._state["pending_next"] = contact

    def record_handoff(self, from_role, to_role, frequency) -> None:
        with self._lock:
            self._state["handoffs"].append({
                "from": from_role, "to": to_role,
                "frequency": frequency, "ts": time.time(),
            })
            self._state["handoffs"] = self._state["handoffs"][-10:]

    # ── 请求校验 ────────────────────────────────────────────────────────────

    def check_request(self, text, tuned_role=None):
        """
        检查飞行员的请求是否该由当前守听的管制员处理。
        返回 {"allowed": bool, "action": str|None, "redirect": {...}|None}
        """
        action = detect_intent(text)
        if not action:
            return {"allowed": True, "action": None, "redirect": None}

        with self._lock:
            phase = self._state["phase"]
            phase_idx = PHASES.index(phase)

        owner = ACTION_OWNER.get(action)
        if action == "runway_request":
            # 跑道由放行指定；起飞前的地面/塔台可以批准更换，降落后由进近指定落地跑道
            with self._lock:
                phase = self._state["phase"]
                descending = self._state["descending"]
            if descending or phase in ("APPROACH", "TOWER_ARR"):
                owner = "APPROACH"
            elif phase in ("GROUND_DEP", "TOWER_DEP"):
                return {"allowed": True, "action": action, "redirect": None}
            else:
                owner = "CLEARANCE"
            return {
                "allowed": False,
                "action": action,
                "redirect": self.contact_for(owner),
                "reason": "runway_not_yet_assigned",
            }
        if owner is None:
            return {"allowed": True, "action": action, "redirect": None}
        owner_idx = PHASES.index(owner)

        # 已经滑行/起飞了才来要放行，而且从没拿到过放行 -> 送回放行
        if action == "ifr_clearance" and phase_idx > PHASES.index("CLEARANCE"):
            if not (self.get("squawk") or self.get("runway")):
                return {
                    "allowed": False,
                    "action": action,
                    "redirect": self.contact_for("CLEARANCE"),
                    "reason": "clearance_missing",
                }

        # 前置条件：没放行就不能进跑道/起飞；没推出/非站立机位不能滑行（G2）。
        # prereq 可以是字段名或 callable(session)->bool（返回 True 表示未满足）。
        for prereq in ACTION_PREREQS.get(action, ()):
            missing = prereq(self) if callable(prereq) else not self.get(prereq)
            if missing:
                # callable 没有字段名；taxi 的缺失项统一叫 pushback
                reason_field = prereq if isinstance(prereq, str) else (
                    "pushback" if action == "taxi" else action)
                return {
                    "allowed": False,
                    "action": action,
                    "redirect": self.contact_for(PREREQ_REDIRECT.get(action, "CLEARANCE")),
                    "reason": f"missing_{reason_field}",
                }

        if owner_idx > phase_idx:
            # 安全阀：飞行员确实守听在该指令所属的台上，只是阶段还没跟上
            #（例如频率库没认出放行台），这时放行并把阶段补上，避免死循环劝退。
            if tuned_role and PHASE_ROLE.get(owner) == tuned_role:
                self.advance_to(owner)
                return {"allowed": True, "action": action, "redirect": None}
            return {
                "allowed": False,
                "action": action,
                "redirect": self.contact_for(owner),
                "reason": "out_of_order",
            }

        expected_role = PHASE_ROLE[phase]
        if tuned_role and expected_role and tuned_role != expected_role:
            return {
                "allowed": False,
                "action": action,
                "redirect": self.contact_for(phase),
                "reason": "wrong_station",
            }

        return {"allowed": True, "action": action, "redirect": None}

    # ── 直接回答（不过 LLM） ────────────────────────────────────────────────

    def answer_frequency_query(self, text, callsign="") -> str | None:
        """"离场频率是多少"这类问题直接查表回答。"""
        if not text or not _FREQ_QUERY.search(text):
            return None
        role = role_from_text(text)
        contact = None
        if role:
            contact = {
                "role": role,
                "role_zh": ROLE_LABEL_ZH.get(role, role),
                "frequency": self.frequency_for(role),
            }
        if not contact or not contact.get("frequency"):
            contact = self.next_contact()
        if not contact or not contact.get("frequency"):
            return None
        role_zh = contact.get("role_zh") or contact.get("role")
        prefix = f"{callsign}，" if callsign else ""
        return f"{prefix}{role_zh}频率{contact['frequency']}。"

    # ── Prompt 片段 ─────────────────────────────────────────────────────────

    def sequence_block(self) -> str:
        with self._lock:
            phase = self._state["phase"]
            lines = []
            for idx, name in enumerate(PHASES):
                role = PHASE_ROLE[name]
                if role is None:
                    continue
                contact = self.contact_for(name)
                freq = contact["frequency"] if contact else None
                marker = "[当前]" if name == phase else ("[ ]" if idx > PHASES.index(phase) else "[x]")
                freq_text = freq if freq else "频率未知"
                lines.append(f"  {marker} {idx}. {PHASE_LABEL_ZH[name]} / {PHASE_LABEL_EN[name]} — {freq_text}")
            current = self.contact_for(phase)
        body = "\n".join(lines)
        nxt = self.next_contact()
        if not nxt:
            next_text = "NEXT CONTACT AFTER YOU: none — this is the last station for this flight."
        elif nxt.get("frequency"):
            next_text = (
                f"NEXT CONTACT AFTER YOU: {nxt['role']} {nxt['frequency']} "
                f"(中文：联系{nxt['role_zh']} {nxt['frequency']})"
            )
        else:
            next_text = (
                f"NEXT CONTACT AFTER YOU: {nxt['role']} (frequency unavailable in the airport "
                "database — say 'contact <facility>, good day' without inventing a frequency)"
            )
        pdc_note = ""
        with self._lock:
            if self._state["phase"] == "DISPATCH":
                pdc_note = (
                    "PHASE NOTE: DISPATCH/签派 — the predeparture clearance (PDC) is delivered via "
                    "ACARS/CPDLC data link, NOT on a voice frequency. The 121.x figure shown for "
                    "Dispatch is the Clearance Delivery voice frequency for display only; the pilot "
                    "submits the PDC request on the data link and reads it back to advance.\n"
                )
        return (
            "CONTACT SEQUENCE (strict order — the pilot must never skip a station):\n"
            f"{pdc_note}"
            f"{body}\n"
            f"{next_text}\n"
            "HANDOFF RULE: when you release this aircraft, your transmission MUST end with the handoff "
            "to the NEXT CONTACT above, and the frequency MUST be included. "
            "Never say 'contact <facility>' without a frequency."
        )

    def authority_block(self) -> str:
        with self._lock:
            phase = self._state["phase"]
        role = PHASE_ROLE[phase] or "ATC"
        allowed = sorted(PHASE_AUTHORITY.get(phase, set()))
        if allowed:
            may = ", ".join(allowed)
        else:
            may = "nothing — you only broadcast information"
        forbidden = sorted({a for p, acts in PHASE_AUTHORITY.items() for a in acts} - set(allowed))
        return (
            f"YOUR AUTHORITY (current phase: {phase} / {PHASE_LABEL_ZH[phase]}):\n"
            f"  YOU MAY issue: {may}\n"
            f"  YOU MAY NOT issue: {', '.join(forbidden)}\n"
            "  If the pilot asks for something you may NOT issue, do NOT improvise and do NOT "
            "perform it anyway. Reply with exactly one line and nothing else: "
            "\"<callsign>，请先联系<拥有该指令的管制单位> <其频率>，再见。\""
        )

    def state_block(self) -> str:
        with self._lock:
            assigned = dict(self._state["assigned"])
        if not assigned:
            return ""
        lines = []
        for field, en, zh in self.PROMPT_FIELDS:
            entry = assigned.get(field)
            if not entry:
                continue
            value = entry["value"]
            if field in ("runway", "arrival_runway"):
                value = runway_display(value, "ascii")
            lines.append(f"  - {zh} / {en}: {value} (assigned by {entry.get('by') or 'ATC'})")
        if not lines:
            return ""
        return (
            "AUTHORITATIVE FLIGHT STATE (already assigned to this aircraft by other controllers):\n"
            + "\n".join(lines)
            + "\n  NEVER contradict these values and NEVER re-issue them. "
              "If the pilot asks what their squawk/runway/altitude is, read back exactly these values."
        )


# ── 输出兜底：不管 LLM 产出什么，权威值与移交频率都以代码保证 ─────────────────


# ── 输出兜底：不管 LLM 产出什么，权威值与移交频率都以代码保证 ─────────────────

_FREQ_RE = re.compile(r"\b1[0-3]\d\.\d{1,3}\b")
_HANDOFF_MARK = re.compile(r"联系|联络|移交|换频|frequency change|contact|再见|good ?day", re.I)
# 终止性许可：说完这句，当前管制员的工作就结束了，必须告诉飞行员下一个联络人。
# 故意不收纳“允许推出/滑行”——那些之后还要继续跟同一个台讲话。
_RELEASE_MARK = re.compile(
    r"放行有效|可以起飞|允许起飞|准许起飞|"
    r"可以着陆|允许着陆|准许着陆|"
    r"可以落地|允许落地|准许落地|"
    r"可以进近|允许进近|准许进近|"
    r"雷达已识别|radar contact|cleared for takeoff|cleared to land|"
    r"cleared for the approach|cleared for approach|cleared to taxi", re.I)
_CLOSING = re.compile(r"[\s、,，。.]*(?:再见|good ?day)\s*[。.!！]?\s*$", re.I)

_ROLE_ALIAS_INDEX = {role: tuple(aliases) for role, aliases in ROLE_TEXT_ALIASES}

# 跑道写法的各种形态：(正则, 形态, 渲染风格)
#   prefix → "runway 02R" / "跑道20R" / "跑道二十右"
#   suffix → "02R runway" / "36左跑道" / "二十右跑道"
_RUNWAY_PATTERNS = [
    (re.compile(r"((?:runway|rwy)\s+)(\d{1,2})\s*([LRC])?", re.I), "prefix", "ascii"),
    (re.compile(r"(\d{1,2})\s*([LRC])?\s+(runway|rwy)\b", re.I), "suffix", "ascii"),
    (re.compile(r"(跑道\s*)(\d{1,2})\s*([LRC左右中])?"), "prefix", "cn_digits"),
    (re.compile(r"(跑道\s*)([零一二三四五六七八九十]{1,3})\s*([左右中])?"), "prefix", "zh_num"),
    (re.compile(r"(\d{1,2})\s*([左右中])\s*(跑道)"), "suffix", "cn_digits"),
    (re.compile(r"([零一二三四五六七八九十]{1,3})\s*([左右中])\s*(跑道)"), "suffix", "zh_num"),
]

_CN_SUFFIX = {"L": "左", "R": "右", "C": "中", "": ""}
_SUFFIX_TO_ASCII = {"L": "L", "R": "R", "C": "C",
                   "左": "L", "右": "R", "中": "C", "": ""}


def _runway_render(canonical: str, style: str) -> str:
    if not canonical:
        return ""
    if style == "ascii":
        return canonical
    m = re.match(r"^(\d{1,2})([LRC]?)$", canonical)
    if not m:
        return canonical
    number, suffix = m.group(1), m.group(2)
    if style == "cn_digits":
        return f"{number}{_CN_SUFFIX[suffix]}"
    return runway_display(canonical, "zh_num")


def _rewrite_runway(text: str, canonical: str) -> str:
    key = normalize_runway(canonical) or canonical
    for pattern, kind, style in _RUNWAY_PATTERNS:
        def repl(match, kind=kind, style=style):
            if kind == "prefix":
                lead, digits, suffix = match.group(1), match.group(2), match.group(3) or ""
                if style == "zh_num":
                    found = normalize_runway(digits + suffix)
                else:
                    found = normalize_runway(digits + _SUFFIX_TO_ASCII.get(suffix, suffix))
                if not found or found == key:
                    return match.group(0)
                return lead + _runway_render(canonical, style)
            digits, suffix, noun = match.group(1), match.group(2) or "", match.group(3)
            if style == "zh_num":
                found = normalize_runway(digits + suffix)
            else:
                found = normalize_runway(digits + _SUFFIX_TO_ASCII.get(suffix, suffix))
            if not found or found == key:
                return match.group(0)
            return _runway_render(canonical, style) + noun

        text = pattern.sub(repl, text)
    return text


def _rewrite_squawk(text: str, squawk: str) -> str:
    pattern = re.compile(r"((?:squawk|应答机)\s*[:：]?\s*)([0-9]{4})", re.I)

    def repl(match):
        if match.group(2) == squawk:
            return match.group(0)
        return match.group(1) + squawk

    return pattern.sub(repl, text)


def _alias_match(tail: str, role: str):
    lower = tail.lower()
    best = None
    for alias in _ROLE_ALIAS_INDEX.get(role, ()):
        idx = lower.find(alias.lower())
        if idx != -1 and (best is None or idx < best[0]):
            best = (idx, len(alias))
    return best


def _find_handoff_span(text: str):
    for match in re.finditer(r"联系|联络|contact", text, re.I):
        tail = text[match.end(): match.end() + 24]
        role = role_from_text(tail)
        if role:
            return match, role
    return None, None


def _fix_handoff_frequency(text: str, session) -> str:
    state = session.state
    match, role = _find_handoff_span(text)
    if role:
        expected = session.frequency_for(role)
        if not expected:
            return text
        tail_start = match.end()
        tail = text[tail_start: tail_start + 28]
        alias = _alias_match(tail, role)
        if not alias:
            return text
        alias_idx, alias_len = alias
        found = re.search(r"(\s*(?:on\s*)?)(\d{3}\.\d{1,3})", tail[alias_idx:])
        if found:
            if found.group(2) == expected:
                return text
            start = tail_start + alias_idx + found.start(2)
            end = tail_start + alias_idx + found.end(2)
            return text[:start] + expected + text[end:]
        joiner = " on " if match.group(0).lower().startswith("contact") else " "
        insert_at = tail_start + alias_idx + alias_len
        return text[:insert_at] + joiner + expected + text[insert_at:]

    # 只有"再见 / good day"却没有频率：补上下一联系人和频率
    if _FREQ_RE.search(text):
        return text
    if state.get("phase") in ("ATIS", "PARKED"):
        return text
    nxt = session.next_contact()
    if not nxt or not nxt.get("frequency"):
        return text
    is_cjk = bool(re.search(r"[㐀-䶿一-鿿]", text))
    if is_cjk:
        phrase = f"，联系{nxt['role_zh']} {nxt['frequency']}"
    else:
        phrase = f", contact {nxt['role']} on {nxt['frequency']}"
    closing = _CLOSING.search(text)
    if closing:
        return text[:closing.start()] + phrase + text[closing.start():]
    # 原文已经以句号结尾时，先掉标点再接，避免出现“。，联系…”这种叠句号。
    stripped = text.rstrip()
    if stripped and stripped[-1] in "。！？.!?":
        stripped = stripped[:-1]
    return stripped + phrase + ("。" if is_cjk else ".")

def enforce_message(text: str, session):
    """
    对 ATC 输出做确定性兜底，返回 (new_text, notes)。
    1) 应答机/跑道与权威值不一致时改写为权威值；
    2) 移交指令缺频率或频率不对时补齐/纠正。
    """
    notes = []
    out = text or ""
    if not out.strip():
        return out, notes

    state = session.state
    assigned = state.get("assigned", {}) or {}
    phase = state.get("phase", "ATIS")
    descending = state.get("descending", False)

    squawk = assigned.get("squawk")
    if squawk:
        new = _rewrite_squawk(out, squawk["value"])
        if new != out:
            notes.append(f"squawk->{squawk['value']}")
            out = new

    # 降落一侧只认进近指定的落地跑道，没指定就不改写，避免把落地跑道改成起飞跑道
    runway = assigned.get("arrival_runway") if (descending or phase in _ARRIVAL_PHASES) else assigned.get("runway")
    if runway:
        new = _rewrite_runway(out, runway["value"])
        if new != out:
            notes.append(f"runway->{runway['value']}")
            out = new

    if _HANDOFF_MARK.search(out) or _RELEASE_MARK.search(out):
        new = _fix_handoff_frequency(out, session)
        if new != out:
            notes.append("handoff_freq")
            out = new

    return out, notes
