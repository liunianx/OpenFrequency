"""
owned_traffic_controller.py — 自有 AI 起飞控制器（混合自有交通计划 P3-A，
见 docs/hybrid-owned-traffic-plan.md §4 P3）。

A 路径（P0-12 实机实测成立）的驱动模型：

  创建（注入器）           放行门控（本模块）              内建 AI-ATC
  ┌──────────────┐   owned_traffic_cleared   ┌──────────────┐
  │ ParkedATC    │ ────────────────────────▶ │ set_flight_plan│
  │ 不带飞行计划  │   （sequencer 判定可放行）  │ 赋予起飞计划   │
  │ = 永停原地等  │                           └──────┬───────┘
  └──────────────┘                                  ▼
                            约 90-120s 排班延迟后自主推出/滑行/起飞
                            （P0-12：静止 ~93s 后开始持续移动）

  回收（本模块 1Hz tick）：距首个已知位置 > despawn_on_airborne_nm（§5 配置，
  默认 5NM）→ despawn + emit owned_traffic_departed。

为什么"赋予计划的时机"就是门控：P0-1 实测不带计划的 ParkedATC 飞机永停；
P0-12 实测带上计划 90-120s 后自主滑行。因此"放行前不给计划、放行时才给"
天然实现"按放行时机指挥起飞"——无需与内建 AI-ATC 抢控制权。
"""
from __future__ import annotations

import math
import threading
import time

try:  # 与 traffic_manager/injector 一致：总线缺失/异常不能拖垮控制器
    from .context import event_bus
except Exception:  # pragma: no cover - 上下文模块理论上总在
    event_bus = None

# tick 周期（秒）：位置轮询 1Hz 足够（despawn 判定是分钟级过程）
_TICK_SECONDS = 1.0
# despawn_on_airborne_nm 缺省（计划 §5）
DEFAULT_DESPAWN_NM = 5.0


def _haversine_nm(p1, p2) -> float:
    """(lat, lon) 两点间大圆距离（海里）。与 departure_sequencer 同款算法。"""
    lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(a)) / 1852.0


class OwnedTrafficController:
    """放行门控 + 离场回收。与注入器（SimObject 生命周期）分工：

    - 注入器：创建/销毁/去重/回包（低频 + 线程）
    - 本控制器：事件驱动的放行动作 + 1Hz 位置轮询回收（策略）

    用法：
        controller = OwnedTrafficController(config, injector)
        controller.start()          # 订阅 owned_traffic_cleared + tick 线程
        ...
        controller.stop()
    """

    def __init__(self, config=None, injector=None):
        owned_cfg = ((config or {}).get("traffic", {}) or {}).get("owned", {}) or {}
        try:
            self.despawn_airborne_nm = float(
                owned_cfg.get("despawn_on_airborne_nm", DEFAULT_DESPAWN_NM))
        except (TypeError, ValueError):
            self.despawn_airborne_nm = DEFAULT_DESPAWN_NM
        self._injector = injector
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        # owned_id -> (lat, lon)：该架首个已知位置（回收距离的基准）
        self._base_positions = {}

    # ── 生命周期 ────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """订阅放行事件并启动 tick 线程。注入器缺失时返回 False（降级）。"""
        if self._thread is not None:
            return True
        if self._injector is None:
            print("OwnedTrafficController: no injector attached; disabled")
            return False
        if event_bus is not None:
            event_bus.on("owned_traffic_cleared", self._on_cleared)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="OwnedTrafficController", daemon=True)
        self._thread.start()
        print("OwnedTrafficController: started "
              f"(despawn_on_airborne_nm={self.despawn_airborne_nm})")
        return True

    def stop(self):
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.5)
        self._thread = None
        with self._lock:
            self._base_positions.clear()

    # ── 放行门控 ────────────────────────────────────────────────────────────

    def _on_cleared(self, payload):
        """owned_traffic_cleared {owned_id, runway, flight_plan} → 赋予计划。

        由 sequencer（logic_manager 接线，P3-A-3）在判定某自有 AI 排到
        可放行时发射。flight_plan 缺省时打日志跳过（调用方负责提供）。
        """
        injector = self._injector
        if injector is None:
            return
        payload = payload or {}
        owned_id = payload.get("owned_id")
        pln = payload.get("flight_plan")
        if not owned_id or not pln:
            print("OwnedTrafficController: cleared 事件缺 owned_id/"
                  "flight_plan，忽略")
            return
        print(f"OwnedTrafficController: {owned_id} cleared for "
              f"{payload.get('runway')} → set_flight_plan({pln!r})")
        injector.set_flight_plan(owned_id, pln)

    # ── 离场回收 tick ───────────────────────────────────────────────────────

    def _loop(self):
        injector = self._injector
        while not self._stop_event.is_set():
            try:
                self._tick(injector)
            except Exception as e:  # noqa: BLE001 — tick 失败不影响下次
                print(f"OwnedTrafficController: tick error — {e!r}")
            time.sleep(_TICK_SECONDS)

    def _tick(self, injector):
        for rec in injector.list_owned():
            if rec.get("state") != "active" or rec.get("object_id") is None:
                continue
            owned_id = rec.get("owned_id")
            with self._lock:
                base = self._base_positions.get(owned_id)
            pos = rec.get("position")
            if base is None:
                # 首个已知位置作为基准。没位置先发一次轮询（下 tick 才有）。
                if pos:
                    with self._lock:
                        self._base_positions[owned_id] = (pos[0], pos[1])
                else:
                    injector.request_owned_position(owned_id)
                continue
            if pos is None:
                injector.request_owned_position(owned_id)
                continue
            self._maybe_despawn(injector, owned_id, rec, base, pos)

    def _maybe_despawn(self, injector, owned_id, rec, base, pos):
        distance_nm = _haversine_nm(base, (pos[0], pos[1]))
        if distance_nm < self.despawn_airborne_nm:
            return
        callsign = rec.get("callsign")
        print(f"OwnedTrafficController: {owned_id} ({callsign}) 离场 "
              f"{distance_nm:.1f}NM ≥ {self.despawn_airborne_nm} → 回收")
        injector.despawn(owned_id)
        with self._lock:
            self._base_positions.pop(owned_id, None)
        self._emit("owned_traffic_departed", {
            "owned_id": owned_id,
            "callsign": callsign,
            "distance_nm": round(distance_nm, 2),
        })

    @staticmethod
    def _emit(name, payload):
        if event_bus is None:
            return
        try:
            event_bus.emit(name, payload)
        except Exception as e:  # noqa: BLE001 — 消费者故障不影响控制器
            print(f"OwnedTrafficController: emit {name} failed — {e!r}")
