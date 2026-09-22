"""
gate_assigner.py — 到达侧停机位/廊桥分配（A3）。

输入来自 ground layout 的 startup_locations（含 gate_id / operation / type /
lat / lon），输出 {"stand": gate_id, "lat":…, "lon":…, "reason":…}。

占用判定：任一交通目标位于 stand 坐标半径 OCCUPY_RADIUS_M 内且 on_ground 即视为
占用；X-Plane/mock 等无交通数据的现场跳过占用检查（调用方传空集合即可）。

分配策略（确定性，无需 LLM）：
1. 过滤掉被占用的 stand；
2. 按 operation 优先级取第一批（Gates/Cargo/Ramp …，重机型优先 Gates）；
3. 同优先级内按 gate_id 稳定排序，取第一个——保证同一输入永远得到同一结果。
"""
from __future__ import annotations

# stand 占用判定半径（米）
OCCUPY_RADIUS_M = 40.0

# operation 优先级（值越小越优先）；未列出的类型排最后
_OPERATION_PRIORITY = {
    "gates": 0,
    "gate": 0,
    "cargo": 1,
    "ramp": 2,
}


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    import math
    radius_m = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius_m * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _priority(stand: dict) -> int:
    operation = str(stand.get("operation") or stand.get("type") or "").strip().lower()
    return _OPERATION_PRIORITY.get(operation, 3)


def compute_occupied_stands(startup_locations, aircraft_list) -> set:
    """返回被占用的 stand gate_id 集合。

    aircraft_list: 可迭代的 dict，需含 lat/lon/on_ground（traffic_manager 的
    AircraftTrackingData 或同名字典均可）。
    """
    occupied = set()
    targets = [a for a in (aircraft_list or []) if getattr(a, "on_ground", None) or a.get("on_ground")]
    if not targets:
        return occupied
    for stand in startup_locations or []:
        gate_id = stand.get("gate_id") or stand.get("name")
        lat, lon = stand.get("lat"), stand.get("lon")
        if not gate_id or lat is None or lon is None:
            continue
        for ac in targets:
            ac_lat = getattr(ac, "latitude", None)
            ac_lon = getattr(ac, "longitude", None)
            if ac_lat is None and isinstance(ac, dict):
                ac_lat, ac_lon = ac.get("lat") or ac.get("latitude"), ac.get("lon") or ac.get("longitude")
            if ac_lat is None or ac_lon is None:
                continue
            try:
                if _haversine_m(lat, lon, float(ac_lat), float(ac_lon)) <= OCCUPY_RADIUS_M:
                    occupied.add(gate_id)
                    break
            except (TypeError, ValueError):
                continue
    return occupied


def assign_gate(startup_locations, aircraft_size="medium", occupied=None):
    """确定性分配一个停机位。无可用停机位时返回 None。

    aircraft_size: 'heavy'/'large' 优先 Gates；其余类型同样按优先级取。
    occupied: compute_occupied_stands 的输出（可为 None/空集 → 跳过占用检查）。
    """
    stands = [s for s in (startup_locations or [])
              if (s.get("gate_id") or s.get("name")) and s.get("lat") is not None]
    if not stands:
        return None

    occupied = occupied or set()
    candidates = [s for s in stands if (s.get("gate_id") or s.get("name")) not in occupied]
    if not candidates:
        # 全部占用：退回全量列表并在 reason 里标注，绝不返回"没停机位"
        candidates = stands
        reason_suffix = " (all stands occupied — nearest-fit fallback)"
    else:
        reason_suffix = ""

    size = (aircraft_size or "medium").lower()
    if size in ("heavy", "large"):
        candidates = [s for s in candidates if _priority(s) == 0] or candidates

    candidates.sort(key=lambda s: (_priority(s), str(s.get("gate_id") or s.get("name"))))
    pick = candidates[0]
    gate_id = pick.get("gate_id") or pick.get("name")
    return {
        "stand": gate_id,
        "lat": pick.get("lat"),
        "lon": pick.get("lon"),
        "reason": f"matched operation {_priority_name(_priority(pick))}{reason_suffix}",
    }


def _priority_name(priority: int) -> str:
    return {0: "gates", 1: "cargo", 2: "ramp"}.get(priority, "other")
