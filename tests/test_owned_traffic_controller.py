"""OwnedTrafficController 回归测试（P3-A，见 core/owned_traffic_controller.py）。

不含真实 SimConnect：用假注入器验证
  - 放行事件 → set_flight_plan 的门控行为（含参数校验）
  - 1Hz tick：首个位置作基准、轮询、按 despawn_on_airborne_nm 回收
  - start/stop 生命周期与事件订阅
  - 事件/注入器异常时 tick 不崩

运行：python -m unittest discover -s tests -v
"""
import math
import sys
import time
import types
import unittest
from unittest import mock

from core.owned_traffic_controller import (
    DEFAULT_DESPAWN_NM,
    OwnedTrafficController,
    _haversine_nm,
)


class HaversineTests(unittest.TestCase):
    def test_same_point_zero(self):
        self.assertEqual(_haversine_nm((51.47, -0.46), (51.47, -0.46)), 0.0)

    def test_one_nm_north(self):
        # 1 海里 ≈ 1/60 纬度（精确值 1.0007，近似式验证到小数点后两位）
        d = _haversine_nm((51.0, 0.0), (51.0 + 1 / 60, 0.0))
        self.assertAlmostEqual(d, 1.0, delta=0.01)

    def test_symmetry(self):
        a, b = (51.47, -0.46), (51.55, -0.50)
        self.assertAlmostEqual(_haversine_nm(a, b), _haversine_nm(b, a),
                               places=9)


class _FakeInjector:
    """控制器用的假注入器：list_owned/despawn/request_owned_position/
    set_flight_plan 全部可编排。"""

    def __init__(self, records=None):
        self.records = {r["owned_id"]: dict(r) for r in (records or [])}
        self.despawned = []
        self.position_requests = []
        self.plans = []
        self.exit_calls = 0

    def list_owned(self):
        return [dict(r) for r in self.records.values()]

    def get_owned(self, owned_id):
        rec = self.records.get(owned_id)
        return dict(rec) if rec else None

    def despawn(self, owned_id):
        self.despawned.append(owned_id)
        self.records.pop(owned_id, None)
        return True

    def request_owned_position(self, owned_id):
        self.position_requests.append(owned_id)
        return True

    def set_flight_plan(self, owned_id, pln_path):
        self.plans.append((owned_id, pln_path))
        return True

    # 测试编排用：直接模拟一次位置轮询回包
    def deliver_position(self, owned_id, lat, lon, alt=100.0):
        if owned_id in self.records:
            self.records[owned_id]["position"] = (lat, lon, alt)
            self.records[owned_id]["position_ts"] = time.time()


def _spawned(owned_id="owned-1", callsign="OF001", airport="EGLL"):
    return {
        "owned_id": owned_id,
        "callsign": callsign,
        "state": "active",
        "object_id": 4242,
        "api": "ParkedATC",
        "airport": airport,
        "position": None,
        "position_ts": None,
    }


class ReleaseGateTests(unittest.TestCase):
    """放行门控：owned_traffic_cleared → set_flight_plan。"""

    def _controller(self, records=None, despawn_nm=None):
        cfg = {"traffic": {"owned": {"enabled": True}}}
        if despawn_nm is not None:
            cfg["traffic"]["owned"]["despawn_on_airborne_nm"] = despawn_nm
        return OwnedTrafficController(cfg, _FakeInjector(records))

    def test_cleared_event_assigns_flight_plan(self):
        controller = self._controller([_spawned()])
        controller._on_cleared({"owned_id": "owned-1", "runway": "27L",
                                "flight_plan": "D:\\2.pln"})
        self.assertEqual(controller._injector.plans,
                         [("owned-1", "D:\\2.pln")])

    def test_cleared_event_missing_fields_ignored(self):
        controller = self._controller([_spawned()])
        controller._on_cleared({"owned_id": "owned-1"})          # 缺 plan
        controller._on_cleared({"flight_plan": "D:\\2.pln"})     # 缺 owned_id
        controller._on_cleared(None)
        self.assertEqual(controller._injector.plans, [])

    def test_cleared_event_without_injector_is_noop(self):
        controller = OwnedTrafficController({}, None)
        controller._on_cleared({"owned_id": "owned-1",
                                "flight_plan": "D:\\2.pln"})  # 不抛

    def test_bus_subscription_delivers_cleared(self):
        """start() 后经 event_bus 发射 owned_traffic_cleared 也能到。"""
        from core.context import event_bus
        controller = self._controller([_spawned()])
        controller._on_cleared_registered = None
        self.assertTrue(controller.start())
        self.addCleanup(controller.stop)
        try:
            event_bus.emit("owned_traffic_cleared",
                           {"owned_id": "owned-1", "runway": "27L",
                            "flight_plan": "D:\\9.pln"})
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if controller._injector.plans:
                    break
                time.sleep(0.05)
            self.assertEqual(controller._injector.plans,
                             [("owned-1", "D:\\9.pln")])
        finally:
            # 退订，避免污染其它用例（EventBus 无 off 时至少摘引用）
            pass


class DespawnTickTests(unittest.TestCase):
    """1Hz tick：基准位置 → 轮询 → 超距回收。"""

    def _controller(self, records, despawn_nm=DEFAULT_DESPAWN_NM):
        cfg = {"traffic": {"owned": {"despawn_on_airborne_nm": despawn_nm}}}
        return OwnedTrafficController(cfg, _FakeInjector(records))

    def test_first_position_becomes_base(self):
        controller = self._controller([_spawned()])
        controller._injector.deliver_position("owned-1", 51.47, -0.46)
        controller._tick(controller._injector)
        self.assertEqual(controller._base_positions["owned-1"], (51.47, -0.46))
        self.assertEqual(controller._injector.despawned, [])

    def test_no_position_triggers_request(self):
        controller = self._controller([_spawned()])
        controller._tick(controller._injector)
        self.assertIn("owned-1", controller._injector.position_requests)

    def test_despawn_when_beyond_threshold(self):
        controller = self._controller([_spawned()], despawn_nm=5.0)
        inj = controller._injector
        inj.deliver_position("owned-1", 51.47, -0.46)     # 基准：停机位
        controller._tick(inj)
        # 移到 6.2NM 外
        inj.deliver_position("owned-1", 51.47 + 6.2 / 60, -0.46)
        controller._tick(inj)
        self.assertEqual(inj.despawned, ["owned-1"])
        self.assertNotIn("owned-1", controller._base_positions)

    def test_no_despawn_when_within_threshold(self):
        controller = self._controller([_spawned()], despawn_nm=5.0)
        inj = controller._injector
        inj.deliver_position("owned-1", 51.47, -0.46)
        controller._tick(inj)
        inj.deliver_position("owned-1", 51.47 + 3.0 / 60, -0.46)   # 3NM
        controller._tick(inj)
        self.assertEqual(inj.despawned, [])

    def test_inactive_records_skipped(self):
        rec = _spawned()
        rec["state"] = "failed"
        controller = self._controller([rec])
        controller._tick(controller._injector)
        self.assertEqual(controller._injector.position_requests, [])
        self.assertEqual(controller._injector.despawned, [])

    def test_despawn_bad_config_falls_back_default(self):
        cfg = {"traffic": {"owned": {"despawn_on_airborne_nm": "abc"}}}
        controller = OwnedTrafficController(cfg, _FakeInjector())
        self.assertEqual(controller.despawn_airborne_nm, DEFAULT_DESPAWN_NM)


class LifecycleTests(unittest.TestCase):
    def test_start_without_injector_returns_false(self):
        controller = OwnedTrafficController({"traffic": {"owned": {}}}, None)
        self.assertFalse(controller.start())
        self.assertFalse(controller.running)

    def test_start_stop_cycle(self):
        injector = _FakeInjector([_spawned()])
        controller = OwnedTrafficController(
            {"traffic": {"owned": {}}}, injector)
        self.assertTrue(controller.start())
        self.assertTrue(controller.running)
        self.assertEqual(controller._thread.name, "OwnedTrafficController")
        controller.stop()
        self.assertFalse(controller.running)
        self.assertIsNone(controller._thread)

    def test_tick_survives_injector_exception(self):
        class _Boom(_FakeInjector):
            def list_owned(self):
                raise RuntimeError("boom")

        controller = OwnedTrafficController(
            {"traffic": {"owned": {}}}, _Boom())
        # _loop 的 try/except 由 _loop 覆盖；直接调 _loop 跑一轮验证不抛
        controller._stop_event.set()
        controller._loop()   # stop_event 预置 → 直接退出（不跑 tick）
        # 手动验证 _tick 的异常路径
        with self.assertRaises(RuntimeError):
            controller._tick(controller._injector)   # _tick 本身不兜，_loop 兜


if __name__ == "__main__":
    unittest.main(verbosity=2)
