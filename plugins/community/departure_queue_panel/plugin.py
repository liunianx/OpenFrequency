"""官方示例插件：离场队表面板（D3）。

验证两件长期闲置的能力：
  1. plugin_manager 早已订阅的 'atc_action' 事件链（G4 修复后首次有真实 emit）
  2. on_traffic_update / inject_panel / set_status_bar_item 插件钩子

插件只读 queue 数据做展示，不侵入核心逻辑；核心未启用排队器时面板显示占位。
"""
from core.plugin_api import OpenFrequencyPlugin


class Plugin(OpenFrequencyPlugin):

    PANEL_ID = "departure_queue"

    def on_load(self):
        self._last_action = "—"
        self._queue_count = 0
        self.inject_panel(
            self.PANEL_ID,
            html="<div id='dq-panel' class='small'>等待塔台队列数据…</div>",
            position="sidebar",
            title="离场排队",
            icon="🛫",
        )

    def on_unload(self):
        self.remove_panel(self.PANEL_ID)

    # ── 钩子 ────────────────────────────────────────────────────────────────

    def on_atc_action(self, action: str, params: dict):
        """核心每次下发结构化指令时触发（Tier-0 重定向 / 指令卡片 / 排队结果）。"""
        self._last_action = action or "—"
        detail = ""
        if isinstance(params, dict):
            if params.get("position"):
                detail = f" #{params['position']}"
            elif params.get("value"):
                detail = f" {params['value']}"
        self.set_status_bar_item(
            "atc_action", text=f"🛫 {self._last_action}{detail}")

    def on_traffic_update(self, icao: str, traffic: list):
        """交通表每次批量更新时刷新面板。"""
        count = len(traffic) if isinstance(traffic, list) else 0
        self._queue_count = count
        rows = "".join(
            f"<li>{t.get('callsign', '?')} — {t.get('state', '?')}"
            f"{' / ' + t['rwy'] if t.get('rwy') else ''}</li>"
            for t in (traffic or [])[:8]
        ) or "<li>（无 AI 交通）</li>"
        self.inject_panel(
            self.PANEL_ID,
            html=(f"<div class='small'>机场 {icao or '—'} · 目标 {count} 架</div>"
                  f"<ul class='small mb-0'>{rows}</ul>"),
            position="sidebar",
            title="离场排队",
            icon="🛫",
        )
