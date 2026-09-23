"""官方示例插件：离场队表面板（D3）。

验证两件长期闲置的能力：
  1. plugin_manager 早已订阅的 'atc_action' 事件链（G4 修复后首次有真实 emit）
  2. on_traffic_update / inject_panel / set_status_bar_item 插件钩子

插件只读 queue 数据做展示，不侵入核心逻辑；核心未启用排队器时面板显示占位。
"""
from core.plugin_api import OpenFrequencyPlugin


def _owned_badge(traffic_item) -> str:
    """自有 AI 的"可指挥"角标（混合自有交通 P4，见 §4 P4）。

    traffic_update 的 owned/owned_id 字段由 traffic_manager P2 回灌
    （owned=True 标记注入器自建机）。FSLTL 只读机无此标记，不渲染角标。
    """
    if not isinstance(traffic_item, dict) or not traffic_item.get('owned'):
        return ""
    owned_id = traffic_item.get('owned_id') or ''
    title = f" title='自有 AI（{owned_id}）· 可指挥'" if owned_id else \
            " title='自有 AI · 可指挥'"
    return (" <span class='badge bg-info text-dark'"
            f"{title}>🎯可指挥</span>")


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
        owned_count = sum(1 for t in (traffic or [])
                          if isinstance(t, dict) and t.get('owned'))
        self._queue_count = count
        rows = "".join(
            f"<li>{t.get('callsign', '?')} — {t.get('state', '?')}"
            f"{' / ' + t['rwy'] if t.get('rwy') else ''}"
            f"{_owned_badge(t)}</li>"
            for t in (traffic or [])[:8]
        ) or "<li>（无 AI 交通）</li>"
        owned_hint = (f" · 🎯可指挥 {owned_count} 架"
                      if owned_count else "")
        self.inject_panel(
            self.PANEL_ID,
            html=(f"<div class='small'>机场 {icao or '—'} · 目标 {count} 架"
                  f"{owned_hint}</div>"
                  f"<ul class='small mb-0'>{rows}</ul>"),
            position="sidebar",
            title="离场排队",
            icon="🛫",
        )
