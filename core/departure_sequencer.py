"""
departure_sequencer.py — 离场排队（DepartureSequencer，D1，模拟器无关）。

输入：
  · traffic_manager.aircraft（含 C2 新字段 aircraft_type/wake_category/
    assigned_runway/assigned_parking…）
  · TaxiRouter 图的 runway 邻接节点（runway_links>0）作 holding point 判定，
    不另写几何算法（G5）
  · AI 的 AI_TRAFFIC_ASSIGNED_RUNWAY（MSFS 直读，最可靠）；缺失时用
    "距该跑道 holding point 图距离最近"过滤

输出：每跑道一条队列；request_takeoff_slot(callsign, runway) 返回本机排位 N、
前机、按尾流间隔计算的预计等待秒数。

尾流间隔（ICAO Doc 4444 Amd.9, 2020）：真实时间型间隔是区间（着陆 HEAVY-behind-
SUPER 2 min … LIGHT-behind-HEAVY 3 min；起飞 2–4 min 且取决于是否全跑道起飞）。
Doc 4444 未给出单一固定表，因此这里取区间保守端作为**工程默认值**（可配置
traffic.wake_sep_seconds 整体覆盖），并在 UI 标注"间隔为估算"：
非法规精确值。B757 按 FAA 规定视作 Heavy。
"""
from __future__ import annotations

import math
import re
import threading

# (前机类别, 后机类别) → 秒。缺项 = 无强制尾流间隔，仅用跑道占用冷却。
DEFAULT_WAKE_SEP_SECONDS = {
    ("J", "J"): 120, ("J", "H"): 120, ("J", "M"): 180, ("J", "L"): 180,
    ("H", "H"): 90, ("H", "M"): 120, ("H", "L"): 180,
    ("M", "M"): 60, ("M", "L"): 120,
    ("L", "L"): 60,
}
# 无强制尾流间隔时的跑道占用冷却（秒）
DEFAULT_COOLDOWN_S = 60
# 尾流类别缺失时的保守固定间隔（秒）
UNKNOWN_WAKE_S = 120

_WAKE_RANK = {"L": 0, "M": 1, "H": 2, "J": 3}


def _norm_runway(value) -> str:
    """'RW18'/'18'/'runway 18L' → '18'/'18L'（保留后缀）。"""
    if not value:
        return ""
    text = str(value).strip().upper()
    for prefix in ("RUNWAY", "RWY", "RW"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.lstrip("0") or "0"
    return text


_SWAP_SUFFIX = {"L": "R", "R": "L", "C": "C", "": ""}


def _runway_group(value) -> str:
    """把跑道号归一到"同一物理跑道"键：18 ↔ 36（L↔R 互换）是同一跑道。

    AI 的 assigned_runway 常与塔台用语方向相反（进场用 36、离场用 18），
    排队必须落在同一条队列里。
    """
    text = _norm_runway(value)
    match = re.match(r"^(\d{1,2})([LRC]?)$", text)
    if not match:
        return text
    num = int(match.group(1)) % 36 or 36
    suffix = match.group(2)
    other = (num + 18 - 1) % 36 + 1
    if num <= other:
        return f"{num:02d}{suffix}"
    return f"{other:02d}{_SWAP_SUFFIX[suffix]}"


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    radius_m = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius_m * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class DepartureSequencer:
    """按跑道维护起飞队列；由 traffic_state_change / 阶段变化触发重算。"""

    # 参与排队的地面/起飞状态（TrafficState 名）
    QUEUE_STATES = {"TAXIING", "PUSHBACK", "TAKEOFF_ROLL"}

    def __init__(self, config=None, traffic_manager=None, taxi_router=None):
        self.config = config or {}
        self.traffic_manager = traffic_manager
        self.taxi_router = taxi_router
        self._lock = threading.RLock()
        self._queues = {}          # runway -> [entry dict]
        self._estimates = False    # 是否有估算间隔（供 UI 标注）
        self._override_s = (self.config.get("traffic", {}) or {}).get("wake_sep_seconds")

    # ── 队列构建 ────────────────────────────────────────────────────────────

    def _runway_holding_nodes(self) -> dict:
        """runway 名 → 邻接 taxi 节点 id 列表（复用 _mark_hotspots 的 runway_links）。"""
        out = {}
        router = self.taxi_router
        if router is None or getattr(router, "graph", None) is None:
            return out
        graph = router.graph
        if graph.number_of_nodes() == 0:
            return out
        for node_id, data in graph.nodes(data=True):
            if data.get("runway_links", 0) <= 0:
                continue
            runway = router._runway_for_node(node_id)
            for name in str(runway).split("/"):
                name = name.strip().upper()
                if name:
                    out.setdefault(_runway_group(name), []).append(node_id)
        return out

    def _coarse_wake(self, aircraft_type) -> str:
        """机型粗分（拿不到 wake_category 时）。"""
        text = str(aircraft_type or "").upper()
        if not text:
            return "UNKNOWN"
        if text.startswith(("A38", "B74", "B77", "A34", "A33")):
            return "H"
        if text.startswith(("C1", "C2", "PA", "BE", "TB", "PC", "DA", "SR", "GL")):
            return "L"
        return "M"

    def rebuild(self, own_runway=None, own_position=None):
        """按当前交通重算全部跑道队列。own_position={'lat':..,'lon':..}（本机）。"""
        tm = self.traffic_manager
        router = self.taxi_router
        if tm is None:
            return
        holding = self._runway_holding_nodes()
        # runway -> [(距离米, callsign, entry_source)]
        buckets = {}
        with tm.lock:
            aircraft = list(tm.aircraft.items())
        for callsign, ac in aircraft:
            if not ac.on_ground:
                continue
            if ac.state.name not in self.QUEUE_STATES:
                continue
            assigned = _runway_group(getattr(ac, "assigned_runway", None))
            if assigned:
                buckets.setdefault(assigned, []).append((0.0, callsign, "assigned_runway"))
                continue
            # 无 assigned_runway：归到距其最近的 runway holding point 所在跑道
            if not holding:
                continue
            best = None
            for runway, nodes in holding.items():
                node = self._nearest_graph_node(router, ac, nodes)
                if node is None:
                    continue
                dist = _haversine_m(ac.latitude, ac.longitude,
                                    router.graph.nodes[node]["lat"],
                                    router.graph.nodes[node]["lon"])
                if best is None or dist < best[0]:
                    best = (dist, runway)
            if best is not None:
                buckets.setdefault(_runway_group(best[1]), []).append(
                    (best[0], callsign, "graph_distance"))

        own_norm = _norm_runway(own_runway)
        queues = {}
        estimates = False
        for runway, entries in buckets.items():
            # assigned_runway 的按距 holding point 的距离排序；无法排序的保持稳定
            holding_nodes = holding.get(runway.upper(), [])
            decorated = []
            for idx, (dist, callsign, source) in enumerate(entries):
                ac = dict(aircraft).get(callsign)
                if ac is None:
                    continue
                if source == "graph_distance":
                    sort_key = dist
                else:
                    node = self._nearest_graph_node(router, ac, holding_nodes)
                    if node is not None:
                        sort_key = _haversine_m(
                            ac.latitude, ac.longitude,
                            router.graph.nodes[node]["lat"],
                            router.graph.nodes[node]["lon"])
                    else:
                        sort_key = 1e12
                decorated.append((sort_key, idx, callsign, ac, source))
            decorated.sort(key=lambda item: (item[0], item[1]))
            queue = []
            for pos, (dist_m, _i, callsign, ac, source) in enumerate(decorated, start=1):
                wake = getattr(ac, "wake_category", "UNKNOWN") or "UNKNOWN"
                if wake == "UNKNOWN":
                    wake = self._coarse_wake(getattr(ac, "aircraft_type", ""))
                    estimates = True
                queue.append({
                    "callsign": callsign,
                    "state": ac.state.name,
                    "wake_category": wake,
                    "position": pos,
                    "runway_match": source,
                    "dist_to_holding_m": None if dist_m >= 1e12 else round(dist_m, 1),
                    "eta_to_runway_s": self._eta_to_runway(ac, dist_m),
                })
            queues[runway] = queue
        with self._lock:
            self._queues = queues
            self._estimates = estimates

    @staticmethod
    def _nearest_graph_node(router, ac, node_ids):
        if router is None or not node_ids or ac.latitude in (None, 0.0):
            return None
        best, best_dist = None, None
        for node_id in node_ids:
            node = router.graph.nodes[node_id]
            dist = _haversine_m(ac.latitude, ac.longitude, node["lat"], node["lon"])
            if best_dist is None or dist < best_dist:
                best, best_dist = node_id, dist
        return best

    @staticmethod
    def _eta_to_runway(ac, dist_m):
        if dist_m is None or dist_m >= 1e12:
            return None
        speed = getattr(ac, "airspeed", 0) or 0
        if speed <= 1:
            return None
        return round(dist_m / (speed * 0.514444), 1)

    # ── 查询 ────────────────────────────────────────────────────────────────

    def get_queue(self, runway) -> list:
        with self._lock:
            return list(self._queues.get(_runway_group(runway), []))

    def wake_gap_seconds(self, leader_category, follower_category) -> int:
        if self._override_s:
            try:
                return int(self._override_s)
            except (TypeError, ValueError):
                pass
        leader = leader_category if leader_category in _WAKE_RANK else "UNKNOWN"
        follower = follower_category if follower_category in _WAKE_RANK else "UNKNOWN"
        if leader == "UNKNOWN" or follower == "UNKNOWN":
            return UNKNOWN_WAKE_S
        gap = DEFAULT_WAKE_SEP_SECONDS.get((leader, follower))
        return gap if gap is not None else DEFAULT_COOLDOWN_S

    def request_takeoff_slot(self, callsign, runway) -> dict:
        """本机申请起飞：返回排位/前机/预计等待秒数。

        队列为空 → position=1, wait_seconds=0，调用方直接放行。
        """
        runway_key = _runway_group(runway)
        queue = self.get_queue(runway_key)
        own = next((item for item in queue if item["callsign"] == callsign), None)
        if own is None:
            # 本机不在队里（刚呼叫 / mock 交通无本机）：队尾就是当前位置
            position = len(queue) + 1
            own_category = "UNKNOWN"
        else:
            position = own["position"]
            own_category = own["wake_category"]
        ahead = queue[:position - 1]
        wait_seconds = 0
        if ahead:
            leader = ahead[-1]
            wait_seconds = self.wake_gap_seconds(leader["wake_category"], own_category)
        return {
            "runway": runway_key,
            "position": position,
            "ahead": [item["callsign"] for item in ahead],
            "leader": ahead[-1]["callsign"] if ahead else None,
            "leader_category": ahead[-1]["wake_category"] if ahead else None,
            "own_category": own_category,
            "wait_seconds": wait_seconds,
            "estimated": self._estimates,
        }

    def queue_snapshot(self, runway) -> dict:
        """给 prompt / UI 的紧凑结构。"""
        queue = self.get_queue(runway)
        return {
            "runway": _runway_group(runway),
            "count": len(queue),
            "estimated": self._estimates,
            "queue": queue,
        }
