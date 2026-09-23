"""
owned_traffic_policy.py — 自有 AI 离场策略（混合自有交通计划 P3 自动部分，
见 docs/hybrid-owned-traffic-plan.md §4 P3）。

纯决策模块（无 SimConnect / event_bus 依赖，可单测）：给定当前自有 AI
状态、玩家机场、玩家排队位位，输出动作列表：

  · ("spawn", spec)     —— 生成一架（ParkedATC，不带计划=原地等）
  · ("release", owned_id) —— 放行（logic_manager 转发 owned_traffic_cleared
                            → controller 赋予飞行计划 → AI-ATC 自主滑行）

**时序模型**（P0-12 实测）：spawn 后不带计划的飞机永停；release 后还需
约 90-120s 排班延迟才开始推。因此：
  · spawn 要**早**：玩家第一次请求起飞时就生成（真到放行时它已就绪）；
  · release 要**缓**：`release_interval_s`（默认 180s）内不重复放行，
    避免多机同时推出 volumetric 拥堵，也匹配"前机起飞后再放下一架"。

策略（MVP，保守）：
  1. 玩家请求起飞且活跃自有 AI < max_aircraft 且已知机场 → spawn 一架；
  2. 距上次 release ≥ release_interval_s 且存在未放行的活跃自有 AI →
     release **最年长**的一架（FIFO，它代表"排在你前面起飞的那架"）。
  配置缺 flight_plan 时跳过 release（只 spawn）并打一次日志提示。
"""
from __future__ import annotations

import time

# 呼号池（与 traffic_manager mock 池同源；真实场景由注入器去重避让 FSLTL）
CALLSIGN_PREFIXES = ("CES", "CCA", "CHH", "CXA", "CQN")

DEFAULT_RELEASE_INTERVAL_S = 180.0


def generate_callsign(taken, index=None):
    """生成一个未被占用的呼号。taken：已占用集合（大小写不敏感）。"""
    taken_upper = {str(c).strip().upper() for c in (taken or [])}
    base_index = int(index if index is not None else time.time() % 900 + 100)
    for _ in range(200):
        prefix = CALLSIGN_PREFIXES[base_index % len(CALLSIGN_PREFIXES)]
        number = 100 + (base_index // len(CALLSIGN_PREFIXES)) % 900
        candidate = f"{prefix}{number}"
        if candidate not in taken_upper:
            return candidate
        base_index += 1
    return f"OF{int(time.time()) % 10000}"     # 理论上到不了


class OwnedTrafficPolicy:
    """放行/生成决策机。逻辑无副作用——动作交给调用方（logic_manager）执行。"""

    def __init__(self, config=None):
        owned_cfg = ((config or {}).get("traffic", {}) or {}).get("owned", {}) or {}
        try:
            self.max_aircraft = max(0, int(owned_cfg.get("max_aircraft", 2)))
        except (TypeError, ValueError):
            self.max_aircraft = 2
        try:
            self.release_interval_s = float(
                owned_cfg.get("release_interval_s", DEFAULT_RELEASE_INTERVAL_S))
        except (TypeError, ValueError):
            self.release_interval_s = DEFAULT_RELEASE_INTERVAL_S
        self.flight_plan = owned_cfg.get("flight_plan") or None
        self._last_release_ts = 0.0
        self._no_plan_warned = False
        self._spawn_count = 0

    # ── 决策入口 ────────────────────────────────────────────────────────────

    def on_takeoff_request(self, active_records, airport, player_position=1,
                            taken_callsigns=None):
        """玩家请求起飞时调用。返回动作列表（spawn/release）。

        active_records：injector.list_owned() 的结果（只取 state==active）。
        airport：当前机场 ICAO（空则只做 release 决策，不 spawn）。
        player_position：sequencer 给玩家的排位（1=可直接进跑道）。
        taken_callsigns：额外已占用呼号（如交通表 keys，注入器也会去重）。
        """
        actions = []
        active = [r for r in (active_records or [])
                  if r.get("state") == "active"
                  and r.get("object_id") is not None]

        # 1) 生成：数量未满 + 知道机场 → 早生成（消化 AI-ATC 排班延迟）
        if airport and len(active) < self.max_aircraft:
            taken = set(taken_callsigns or [])
            taken.update(str(r.get("callsign") or "").upper() for r in active)
            self._spawn_count += 1
            callsign = generate_callsign(taken, self._spawn_count)
            actions.append(("spawn", {
                "callsign": callsign,
                "airport": str(airport).strip().upper(),
                # model_title 留空：注入器回落 config default_model_title，
                # 再被 22/34 拒绝时还会用用户当前飞机 title 兜底
                "model_title": None,
            }))

        # 2) 放行：冷却满足 + 有未放行的活跃机 → 放最年长的一架（FIFO）
        pending = [r for r in active if not r.get("flight_plan")]
        if pending:
            if not self.flight_plan:
                if not self._no_plan_warned:
                    self._no_plan_warned = True
                    print("OwnedTrafficPolicy: owned.flight_plan 未配置——"
                          "只生成不放行（无法指挥起飞）。")
            elif time.time() - self._last_release_ts >= self.release_interval_s:
                oldest = min(pending, key=lambda r: r.get("spawned_at") or 0)
                actions.append(("release", oldest["owned_id"]))
                self._last_release_ts = time.time()
        return actions

    def note_released(self):
        """外部（logic_manager）完成 release 后调用，用于冷却计时。"""
        self._last_release_ts = time.time()

    def reset(self):
        """场景切换/断连时重置冷却与计数（不删飞机——那是注入器的事）。"""
        self._last_release_ts = 0.0
        self._no_plan_warned = False
        self._spawn_count = 0
