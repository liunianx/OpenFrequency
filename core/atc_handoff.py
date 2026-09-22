"""
ATC Handoff State Machine
管制移交状态机 - 实现完整的 ATC 移交流程

移交顺序:
ATIS抄收 → 放行 → 地面/机坪 → 塔台起飞 → 离场 → 中心 → 进场 → 塔台降落 → 地面/机坪

本模块现在只是 core.atc_session.ATCSession 的适配层：真正的阶段梯、
权威指令（跑道/应答机/SID/高度）、下一联系人频率都由 ATCSession 统一持有，
这里保留原有的事件接口与对外 API，方便 app.py 与前端继续使用。
"""
from enum import Enum

from .atc_session import (
    ATCSession,
    PHASES,
    PHASE_ROLE,
    PHASE_LABEL_EN,
    PHASE_LABEL_ZH,
)
from .context import shared_context, event_bus


class ATCPhase(Enum):
    """ATC 阶段枚举（与 ATCSession.PHASES 保持一致）"""
    DISPATCH = "DISPATCH"         # 签派（PDC 数据链预放行）
    ATIS = "ATIS"               # 抄收 ATIS
    CLEARANCE = "CLEARANCE"     # 放行
    GROUND_DEP = "GROUND_DEP"   # 地面/机坪 (出发)
    TOWER_DEP = "TOWER_DEP"     # 塔台 (起飞)
    DEPARTURE = "DEPARTURE"     # 离场
    CENTER = "CENTER"           # 中心/区调
    APPROACH = "APPROACH"       # 进场
    TOWER_ARR = "TOWER_ARR"     # 塔台 (降落)
    GROUND_ARR = "GROUND_ARR"   # 地面/机坪 (到达)
    PARKED = "PARKED"           # 完成停机


class ATCHandoffManager:
    """
    ATC 移交状态机管理器（ATCSession 适配层）
    - 自动检测当前飞行阶段
    - 强制移交到正确的管制单位
    - 自动触发 ATIS 抄收
    - 所有跨管制员共享状态由 ATCSession 持有
    """

    def __init__(self, config, socketio, session=None, airport_frequency_service=None):
        self.config = config
        self.socketio = socketio
        self.session = session or ATCSession(config, airport_frequency_service)
        self.session.attach(shared_context)
        self.current_phase = ATCPhase.ATIS
        self.atis_copied = False
        self.clearance_received = False
        self.last_phase = None

        # 航班特定数据（镜像自 session，便于旧代码读取）
        self.origin_icao = None
        self.dest_icao = None
        self.cruise_altitude = 0

        # 订阅事件
        event_bus.on('telemetry_update', self.on_telemetry)
        event_bus.on('flight_plan_loaded', self.on_flight_plan)
        event_bus.on('atis_played', self.on_atis_played)
        event_bus.on('clearance_confirmed', self.on_clearance_confirmed)
        event_bus.on('handoff_complete', self.on_handoff_complete)

        print("ATCHandoffManager: Initialized (ATCSession adapter)")

    # ── 事件处理 ────────────────────────────────────────────────────────────

    def on_flight_plan(self, flight_plan):
        """航班计划加载时初始化"""
        self.session.load_from_flight_plan(flight_plan or {})
        self._sync_mirror()
        self.origin_icao = self.session.state.get('origin')
        self.dest_icao = self.session.state.get('destination')
        self.cruise_altitude = self.session.state.get('cruise_alt', 0)

        # 自动请求 ATIS
        if self.origin_icao:
            print(f"ATCHandoffManager: 自动获取 {self.origin_icao} ATIS...")
            self._request_atis(self.origin_icao)
            self._broadcast_phase_change()

    def on_telemetry(self, data):
        """根据遥测数据检测阶段转换（实际逻辑在 ATCSession）"""
        advanced = self.session.observe_telemetry(
            on_ground=data.get('on_ground'),
            altitude=data.get('altitude'),
            vs=data.get('vs'),
            groundspeed=data.get('groundspeed'),
        )
        if advanced:
            self._sync_mirror()
            self._broadcast_phase_change()

    def _sync_mirror(self):
        phase = self.session.phase
        try:
            self.current_phase = ATCPhase(phase)
        except ValueError:
            self.current_phase = ATCPhase.ATIS

    def _transition_to(self, new_phase):
        """执行阶段转换"""
        if isinstance(new_phase, ATCPhase):
            new_phase = new_phase.value
        self.last_phase = self.current_phase
        if not self.session.advance_to(new_phase):
            return
        self._sync_mirror()
        phase_names = PHASE_LABEL_EN
        print(f"ATCHandoffManager: 阶段转换 → {phase_names.get(new_phase, new_phase)}")

        # 触发主动移交事件
        event_bus.emit('mandatory_handoff', {
            'from_phase': self.last_phase.name if self.last_phase else None,
            'to_phase': new_phase,
            'controller': phase_names.get(new_phase),
            'next_contact': self.session.next_contact(),
        })

        # 如果是进场阶段，自动获取目的地 ATIS
        if new_phase == 'APPROACH' and self.dest_icao:
            print(f"ATCHandoffManager: 自动获取 {self.dest_icao} ATIS...")
            self._request_atis(self.dest_icao)

    def _request_atis(self, icao):
        """请求 ATIS 广播"""
        event_bus.emit('atis_playback_request', icao)

    def _broadcast_phase_change(self):
        """同步镜像阶段。socket 广播统一由 LogicManager._emit_phase_update 负责
        （A4.3：修复 atc_handoff 与 logic_manager 双份 atc_phase_update emit）。"""
        self._sync_mirror()

    def on_atis_played(self, icao):
        """ATIS 播放完成"""
        self.session.mark_atis_copied()
        self.atis_copied = True
        print(f"ATCHandoffManager: ATIS {icao} 已抄收")

    def on_clearance_confirmed(self):
        """放行确认"""
        self.session.mark_atis_copied()
        self.clearance_received = True
        print("ATCHandoffManager: 放行已确认")

    def on_handoff_complete(self, data):
        """处理手动移交完成"""
        target_phase = data.get('phase')
        if target_phase:
            self._transition_to(target_phase)

    def reset(self):
        """重置状态（新航班）"""
        self.session.reset()
        self.atis_copied = False
        self.clearance_received = False
        self.origin_icao = None
        self.dest_icao = None
        self.cruise_altitude = 0
        self._sync_mirror()
        print("ATCHandoffManager: 状态已重置")

    # ── 查询接口 ────────────────────────────────────────────────────────────

    def get_current_controller(self):
        """获取当前应该联系的管制单位"""
        return self.session.phase_role()

    def get_current_phase(self):
        return self.session.phase

    def get_suggested_frequency(self):
        """获取当前阶段建议的频率"""
        contact = self.session.contact_for(self.session.phase)
        if contact and contact.get('frequency'):
            return float(contact['frequency'])
        return 121.5  # 默认紧急

    def get_next_contact(self):
        return self.session.next_contact()

    def manual_advance(self):
        """手动推进到下一阶段（调试用）"""
        nxt = self.session.next_phase()
        if nxt:
            self._transition_to(nxt)
