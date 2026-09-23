# 混合自有交通（Hybrid Owned-Traffic）实施计划

> 目标：在**不触碰 FSLTL**的前提下，让 OpenFrequency 能真正"指挥"少数几架飞机
> 起飞。FSLTL 继续负责背景/环境流量（只读，维持现状）；OpenFrequency 另外
> 自建 1~N 架**自有 AI 飞机**——因为是自己创建的 SimObject，SimConnect 所有权
> 规则允许对其 `SetDataOnSimObject` / 下飞行计划，从而实现"按放行时机滑跑起飞"。
>
> 本文档所有 SimConnect API 名标注了可信度；凡标 **[需实机验证]** 的，离线环境
> 无法确认签名/行为，落地前必须在真实 MSFS 上先跑 P0 spike。

---

## 1. 为什么是"混合"而不是"替代"

结论来自对 FSLTL 发行包的核查（2026-09，injector 2.1.2）：


- FSLTL 注入的飞机由它自己创建，**其 SimObject 所有权在 FSLTL**。外部 SimConnect
  客户端（就是 OpenFrequency）无法可靠 `SetDataOnSimObject` 去移动/控制它们。
  这也是 `docs/SIMULATOR_DATA_MATRIX.md` 记录的现状：我们对 FSLTL 交通**只读**。

因此唯一能"指挥起飞"的合法路径是：**自己创建 AI 飞机**。为把成本压到最低，只
创建"玩家真正会交互的那几架"（离场队列里排在玩家前面的 1~2 架），其余环境流量
照旧交给 FSLTL。

### 与 FSLTL 的边界（硬约束）

- **不共享飞机**：自有 AI 与 FSLTL 飞机是两批独立 SimObject，不能相互接管。
- **避免重影**：自有 AI 的呼号/停机位要与 FSLTL 已占用的错开，否则视觉上会
  出现两架"同名机"。启用时需读一遍 `simconnect_traffic` 的枚举结果做去重。
- **可整体关闭**：新增 `traffic.owned.enabled` 开关，默认 `false`，回退即恢复
  纯 FSLTL 只读行为。

---

## 2. 现状代码盘点（改动的锚点）

| 现有模块 | 角色 | 本计划如何用它 |
|---|---|---|
| `core/simconnect_traffic.py` | 自建 data-def + request，**只读**枚举 AI；已直接调 `self.sc.dll.*` 绕过高层封装 | **复用其模式**：新写入模块同样直接打 `self.sc.dll.*`，共用"独立 SimConnect 实例 + 独立 dispatch 线程"的线程模型 |
| `core/traffic_manager.py` (`TrafficStateManager`) | 消费枚举结果，推断状态，`event_bus.emit('traffic_state_change')` / `'traffic_update'` | 自有 AI 也要进同一张 `aircraft` 表，让 sequencer / chatter 无差别看待 |
| `core/departure_sequencer.py` (`DepartureSequencer`) | 按跑道排队、算尾流间隔、`request_takeoff_slot()` | **控制信号来源**：队列里自有 AI 排到"可放行"时，触发它起飞 |
| `core/logic_manager.py` | 持有 `departure_sequencer`，`rebuild()` / `queue_snapshot()` / `request_takeoff_slot()` | 挂接新的"放行→驱动起飞"回调 |
| `core/context.py` (`event_bus`) | 全局事件总线 | 新增 `owned_traffic_*` 事件 |
| `core/sim_provider*.py` | MSFS/XP provider 抽象 | 写入能力做成 provider 可选扩展；XP 侧先不做（见 §7） |

---

## 3. 目标架构

```
                     ┌───────────────────────────────────────────┐
   FSLTL injector ──▶│ MSFS (SimObjects)                          │
   (环境流量, 只读)   │   · FSLTL 飞机 (owner=FSLTL, 只读)          │
                     │   · 自有 AI    (owner=OpenFrequency, 可写)  │◀── 新增写入
                     └───────────────────────────────────────────┘
        ▲ 只读枚举                                   ▲ 创建/驱动
        │                                            │
  simconnect_traffic (C1)                    OwnedTrafficInjector (新)
        │                                            │
        ▼                                            │
  TrafficStateManager ──── aircraft 表（FSLTL + 自有，统一） 
        │                                            ▲
        ▼                                            │ 起飞指令
  DepartureSequencer ──放行时机──▶ OwnedTrafficController (新)
        │
        ▼
  logic_manager / UI（队列面板，标注哪几架是"可指挥"）
```

新增两个模块：
- **`core/owned_traffic_injector.py`** — 负责创建/销毁自有 AI（SimObject 生命周期）。
- **`core/owned_traffic_controller.py`** — 负责驱动已创建的自有 AI（滑行→进跑道→起飞→初始爬升），并把它的状态喂回 `TrafficStateManager`。

拆两层的原因：创建是低频事件（进场景时几架），驱动是每帧高频；分开便于各自的
线程/失败处理，也便于 P2 只做创建、P3 再做驱动。

---

## 4. 分阶段计划

### P0 — 可行性 Spike（先做，别写业务代码）
目的：在真实 MSFS 上确认下面几个 `self.sc.dll.*` 调用真的存在且行为符合预期。
**这一步不通，后面全部作废，所以必须最先做。**

需验证的 API（python-SimConnect 0.4.8 未封装，直接打 DLL；签名 **[需实机验证]**）：
1. `AICreateNonATCAircraft(szContainerTitle, szTailNumber, InitPos, RequestID)`
   —— 能否在指定停机位/跑道口创建一架，拿到回包里的 `dwObjectID`。
2. `AICreateSimulatedObject` —— 备选（非飞机模型）。
3. `AISetAircraftFlightPlan(ObjectID, szFlightPlanPath, RequestID)` —— 载入 .PLN
   后，内建 AI-ATC 是否会自己滑行起飞（省事路径）。
4. `AIReleaseControl(ObjectID, RequestID)` + `SetDataOnSimObject(...)` —— 夺回
   控制权后逐帧写 `PLANE LATITUDE/LONGITUDE/ALTITUDE/HEADING/GROUND VELOCITY`
   是否即时生效（完全可控路径）。
5. `AIRemoveObject(ObjectID, RequestID)` —— 清理。

**产物**：`tests/spikes/spike_owned_ai.py`（临时脚本，验证完删）+ 在本文件
§9 记录实测结论。**决策点**：优先走 (3) 内建 AI-ATC；若其起飞时机不可控/不可
接受，退到 (4) 自驱动。

### P1 — 注入器骨架 `OwnedTrafficInjector`
- 新建 `core/owned_traffic_injector.py`，仿 `simconnect_traffic.py`：
  - 自建独立 `SimConnect(auto_connect=True)` 实例 + 独立 dispatch 线程（**不复用**
    读通道实例，SimConnect 实例非线程安全）。
  - `spawn(spec) -> owned_id`：`spec` 含 title/机型/尾号/初始位置（停机位或
    跑道等待点）。维护 `{owned_id: {objectID, callsign, state, spec}}`。
  - `despawn(owned_id)` / `despawn_all()`（进程退出、场景切换、开关关闭时全清）。
  - 去重：spawn 前比对 `simconnect_traffic` 最新枚举，避开 FSLTL 已用呼号/停机位。
- 失败一律降级：任一 DLL 调用抛异常 → 记日志、标记该 owned_id 失败、不影响其它。

### P2 — 状态回灌 TrafficStateManager
- 自有 AI 也要出现在 `traffic_manager.aircraft` 里，字段形态与枚举来的 FSLTL
  飞机一致（`aircraft_type`/`wake_category`/`assigned_runway`/经纬度/地速…），
  多打一个 `owned=True` 标记。
- 这样 `DepartureSequencer` 和 `chatter_generator` **无需改判定逻辑**就能把自有
  AI 纳入排队与台词。
- `event_bus.emit('traffic_update')` 的列表里合并两来源（去重后）。

### P3 — 起飞驱动 `OwnedTrafficController`
两条路二选一（由 P0 决策）：
- **A. 内建 AI-ATC 路径**：spawn 时给它 `AISetAircraftFlightPlan`，飞机自己会
  滑行；"指挥起飞"= 用一个门控让它在被放行前停在等待点（如放行前不给含跑道的
  计划 / 或 freeze），放行后再放行。**简单，但起飞时机精度受内建 AI 限制。**
- **B. 自驱动路径**：`AIReleaseControl` 后，controller 每帧按状态机
  （`HOLDING_POINT → LINEUP → TAKEOFF_ROLL → ROTATE → INITIAL_CLIMB`）写
  位置/姿态/地速。滑行几何复用 `core/taxi_router.py` 的图（数据矩阵已说明 MSFS
  地面网靠 LittleNavMap/OSM）。**完全可控，但要自己写简化运动模型。**
- 放行触发：`DepartureSequencer` 判定某自有 AI 排到队首且满足尾流间隔 →
  `event_bus.emit('owned_traffic_cleared', {owned_id, runway})` → controller 执行。
- **建议先做 A 打通闭环**，B 作为"精确模式"后置。

### P4 — 配置、UI、去重与打磨
- 配置项见 §5；离场队列面板（`plugins/community/departure_queue_panel`）里给自有
  AI 加一个"可指挥"角标，区别于只读的 FSLTL 机。
- 场景切换/断连/退出的清理钩子（避免留下孤儿 SimObject）。
- 与 `atc_monitor` 打通：玩家对自有 AI 喊话时的一致性（可选，后置）。

---

## 5. 配置新增（`config.example.json` 的 `traffic` 段）

```jsonc
"traffic": {
  "enabled": true,
  "msfs_ai_enabled": true,          // 现有：FSLTL 只读枚举
  "sequencer_enabled": false,
  "chatter_enabled": true,
  "wake_sep_seconds": null,
  "owned": {                        // 新增
    "enabled": false,               // 总开关，默认关（回退=纯 FSLTL）
    "max_aircraft": 2,              // 最多自建几架（控制成本/性能）
    "drive_mode": "ai_atc",         // "ai_atc"(P3-A，已实证) | "self_drive"(P3-B)
    "default_model_title": "",      // 用哪个已装机模型当外观。
                                   // ⚠ MSFS2024 实测："空=默认AI机"与各 Asobo
                                   // 默认机容器在该类装机上不存在（22/34 拒绝）。
                                   // 填**用户当前飞机的 TITLE simvar 值**（如
                                   // "A350-900 (Default Cabin)"）；注入器被拒时
                                   // 也会自动用用户机 title 兜底重试。
    "despawn_on_airborne_nm": 5,    // 起飞后离场 N NM 即回收，交还想象空间
    "flight_plan": "",              // 放行时赋予自有 AI 的 .pln 路径（P3 自动
                                   // 放行必需；空=只生成不放行）。手动路由
                                   // /api/owned_traffic/clear 可逐架指定。
    "release_interval_s": 180       // 放行冷却：AI-ATC 排班+滑行是分钟级，
                                   // 冷却内不重复放行，避免多机同时推出
  }
}
```

三个回退开关保持"各自独立、互不牵连"的既有约定：`owned.enabled=false` 完全
关闭本特性，不影响 `msfs_ai_enabled` / `sequencer_enabled`。

---

## 6. 涂装/模型问题（重要现实约束）

FSLTL 的上千真实涂装**不能被复用/重分发**（其许可禁止）。自有 AI 的外观只能用
用户 MSFS 里**已安装**的机模：
- MVP：用 `owned.default_model_title` 指定一个通用机模（甚至默认 AI 机），外观
  "素"但功能完整——因为只有 1~2 架，视觉代价可接受。
- 不做涂装库、不打包任何 FSLTL 资产。
- 若用户恰好装了某涂装包，允许在配置里手填 title，但不内置匹配逻辑。

这正是"只造几架"的核心收益：把最贵的涂装/实时数据问题绕开。

---

## 7. X-Plane 侧

本期**不做**。X-Plane 的 TCAS dataref 只读（数据矩阵 §2），自有 AI 需走 XP 自己
的 AI 飞机/插件 API，与 SimConnect 完全不同栈。计划仅覆盖 MSFS/P3D/FSX
（SimConnect 族）。`OwnedTrafficInjector` 通过 provider 能力探测挂载；非 SimConnect
provider 直接不启用本特性。

---

## 8. 风险与回退

| 风险 | 影响 | 缓解 |
|---|---|---|
| P0 中 `AICreate*` 签名/行为与假设不符 | 整个方案基础动摇 | P0 先行，不通则停；退而求其次只做"表现层"增强 |
| 自有 AI 与 FSLTL 呼号/停机位重影 | 视觉出戏 | spawn 前对枚举结果去重；错开停机位 |
| 逐帧 `SetDataOnSimObject`（B 路径）抖动/穿模 | 观感差 | 优先 A 路径；B 作为可选精确模式 |
| 孤儿 SimObject（崩溃/切场景未清理） | 场景残留飞机 | 进程退出/断连/切场景全清；启动时清一遍自有命名空间 |
| 性能（多一个 SimConnect 实例 + dispatch 线程） | CPU/内存 | `max_aircraft` 限流；沿用读通道已验证的线程模型 |
| 与内建 ATC / FSLTL 争抢跑道 | 逻辑冲突 | 自有 AI 起飞逻辑由本项目 sequencer 单点决策 |

**总回退**：`traffic.owned.enabled=false` → 不 spawn 任何飞机，行为完全等同当前
版本（纯 FSLTL 只读）。

---

## 9. 验证清单（**均需实机 MSFS + FSLTL**，离线不可验证）

- [ ] **P0-1** `AICreateNonATCAircraft` 能创建并返回有效 `dwObjectID`。
- [ ] **P0-2** `AISetAircraftFlightPlan` 后内建 AI-ATC 会自主滑行/起飞（判定 A 路径可行性）。
- [ ] **P0-3** `AIReleaseControl` + `SetDataOnSimObject` 逐帧写位置即时生效（判定 B 路径可行性）。
- [ ] **P0-4** `AIRemoveObject` 能干净移除，无残留。
- [ ] **P1** spawn 的自有 AI 出现在 `simconnect_traffic` 枚举里，且与 FSLTL 飞机不重号。
- [ ] **P2** 自有 AI 进入 `DepartureSequencer` 队列，排位与尾流间隔计算正确。
- [ ] **P3** sequencer 放行 → 自有 AI 在正确跑道滑跑起飞；离场 N NM 被回收。
- [ ] **P4** 关闭 `owned.enabled` 后，场景中无任何自有 AI，行为回到升级前。

### P0 实测结论（待填）
> 在真实 MSFS 上跑完 `tests/spikes/spike_owned_ai.py` 后，把每个 API 的实际
> 签名、返回、坑记录在此，作为 P1+ 的事实依据。

D:\fly\OpenFrequency\tests\spikes>py spike_owned_ai.py caps
已连接 SimConnect（独立 client）。
── DLL 能力探测 ──
  ✓ AICreateNonATCAircraft
  ✓ AICreateSimulatedObject
  ✓ AISetAircraftFlightPlan
  ✓ AIReleaseControl
  ✓ AIRemoveObject
  ✓ SetDataOnSimObject
  ✓ CallDispatch
── spike 结束。请把实测结果誊抄进 docs/hybrid-owned-traffic-plan.md §9 ──

D:\fly\OpenFrequency\tests\spikes>py spike_owned_ai.py spawn
已连接 SimConnect（独立 client）。
── DLL 能力探测 ──
  ✓ AICreateNonATCAircraft
  ✓ AICreateSimulatedObject
  ✓ AISetAircraftFlightPlan
  ✓ AIReleaseControl
  ✓ AIRemoveObject
  ✓ SetDataOnSimObject
  ✓ CallDispatch
AICreateNonATCAircraft title=b'Airbus A320 Neo Asobo' tail=OF001 req=9901 …
  调用抛异常：ArgumentError('argument 4: TypeError: expected SIMCONNECT_DATA_INITPOSITION instance instead of SIMCONNECT_DATA_INITPOSITION')  ←— 大概率是签名/结构体对齐不符，记进 §9
未拿到 objectID，无法继续。检查上面的 HRESULT/EXCEPTION 记进 §9。
── spike 结束。请把实测结果誊抄进 docs/hybrid-owned-traffic-plan.md §9 ──

D:\fly\OpenFrequency\tests\spikes>py spike_owned_ai.py plan <PLN>
命令语法不正确。

D:\fly\OpenFrequency\tests\spikes>py spike_owned_ai.py plan drive
已连接 SimConnect（独立 client）。
── DLL 能力探测 ──
  ✓ AICreateNonATCAircraft
  ✓ AICreateSimulatedObject
  ✓ AISetAircraftFlightPlan
  ✓ AIReleaseControl
  ✓ AIRemoveObject
  ✓ SetDataOnSimObject
  ✓ CallDispatch
AICreateNonATCAircraft title=b'Airbus A320 Neo Asobo' tail=OF001 req=9901 …
  调用抛异常：ArgumentError('argument 4: TypeError: expected SIMCONNECT_DATA_INITPOSITION instance instead of SIMCONNECT_DATA_INITPOSITION')  ←— 大概率是签名/结构体对齐不符，记进 §9
未拿到 objectID，无法继续。检查上面的 HRESULT/EXCEPTION 记进 §9。
── spike 结束。请把实测结果誊抄进 docs/hybrid-owned-traffic-plan.md §9 ──

#### P0 首轮复盘（2026-09-22，根因已定位并修复）

**caps 全过（7/7 函数存在）= 方案基础成立。** spawn 两次翻车，根因都不是
"假设不符"，而是调用方式问题，已全部修复：

1. **结构体类身份（实机报的 argument 4）**：python-SimConnect 库的
   `Attributes.py` 给 `AICreateNonATCAircraft` 等声明了 `argtypes`，
   InitPos 参数必须是**库自己的** `SIMCONNECT_DATA_INITPOSITION` 类实例。
   spike/注入器原先本地另定义同名同布局类，ctypes 查的是类身份 →
   `expected SIMCONNECT_DATA_INITPOSITION instance instead of
   SIMCONNECT_DATA_INITPOSITION`（两个类名一模一样，极具迷惑性）。
   → 修复：从 `SimConnect.Enum` 导入库类构造（`_library_initpos_class`）。
2. **0.4.8 的库 argtypes bug**（requirements 锁定的版本）：title/tail 两个
   `c_char_p` 被误声明为 `c_double`，bytes 字符串会直接
   `ArgumentError: must be real number, not bytes`。
   → 修复：注入器 `start()` 一次性把 argtypes 改写为 SDK 正确签名
   （`_repair_ai_create_argtypes`）；新版库已是 c_char_p，改写幂等无害。
3. **ID 一律传纯 int**：库的 ID 类型是 IntEnum/c_uint32 子类，`from_param`
   对 DWORD 实例的兼容性随 Python 版本变化（3.14 上 `int(DWORD实例)`
   直接 ValueError）。纯 int 全版本安全。
4. 另：`plan <PLN>` 在 cmd 里 `<` 被当成重定向——命令行要给真实路径。

修复已进 `tests/spikes/spike_owned_ai.py` 与 `core/owned_traffic_injector.py`，
并新增 6 个回归测试固化（`LibraryArgtypesRegressionTests`：假 DLL 按库真实
argtypes 行为校验，第一轮若有这些测试当场就能抓住）。

**待重跑**（P0-1~4 仍未真验证，清单不勾）：
```
py tests\spikes\spike_owned_ai.py spawn
py tests\spikes\spike_owned_ai.py plan  C:\某个\真实\飞行计划.pln
py tests\spikes\spike_owned_ai.py drive
```
第二轮重点观察：spawn 是否拿到 objectID（P0-1）；plan 模式 MSFS 里那架
是否自己滑行/起飞（P0-2，A 路径判定）；drive 模式逐帧写 GROUND VELOCITY
是否即时生效（P0-3，B 路径判定）。结论继续誊抄进本小节。

#### P0 第二轮实测（2026-09-22，第三坑已修复）

进度：argtypes 修复生效——`AICreateNonATCAircraft` 不再抛 ArgumentError，
返回了 HRESULT。但暴露**第三只同类坑**：

1. **CallDispatch 回调类型身份**：泵消息时报
   `ArgumentError: expected WinFunctionType instance instead of
   WinFunctionType`（argument 2）。库 CallDispatch 的 argtypes 第二参只认
   它自己的 `DispatchProc` 类型，且该类型签名里的 `POINTER(SIMCONNECT_RECV)`
   指向库模块的 RECV 类——本地用同款签名自造的是另一个原型缓存类。
   → 修复：回调用 `type(sc.my_dispatch_proc_rd)`（库实例的类型）包装，
   spike 与 `core/owned_traffic_injector.py._make_dispatch_proc` 均已改。
2. **spawn 见到 HRESULT=-2147467259（0x80004005=E_FAIL）**：泵崩溃前没收到
   EXCEPTION 回包，真实原因未知。修好回调类型后重跑，脚本会把
   SIMCONNECT_EXCEPTION 码翻译出来（22=CREATE_OBJECT_FAILED、
   34=OBJECT_CONTAINER=机模 title 无效、33=REALITY_BUBBLE 外…）。
   E_FAIL 常见诱遇排查清单：
   - **停在主菜单没进世界**（AI 创建需要已加载的场景）；
   - **title 不在装机列表**："Airbus A320 Neo Asobo" 是 2020/2024 默认
     AI 机，若本机改装/精简过内容可能没有——用
     `OF_TITLE="准确的容器名" py spike_owned_ai.py spawn` 试；
   - 位置太离谱（默认给的是 KJFK 跑道口附近，MSFS 不在该机场时也可能拒）。
3. 回归测试已加第三坑用例（`test_dispatch_proc_uses_library_type` /
   `test_module_local_proc_type_rejected_by_call_dispatch`），现共 8 个
   库形态回归用例。

**第三轮待跑**（同第二轮三条命令）：
```
py tests\spikes\spike_owned_ai.py spawn
py tests\spikes\spike_owned_ai.py plan  D:\1.pln
py tests\spikes\spike_owned_ai.py drive
```
跑之前确认：MSFS 已进入世界（能看到机场/飞机，不是主菜单）；接着烦请
把 spawn 的 EXCEPTION 码（如有）誊抄进本小节。

#### P0 第三轮实测（2026-09-22，诊断模式已就位）

结果：CallDispatch 修好了（泵 5s 无崩溃、无异常），但三种模式（spawn /
plan / drive）**全部稳定返回 HRESULT=-2147467259（0x80004005=E_FAIL），
零 ASSIGNED_OBJECT_ID、零 EXCEPTION 回包**。同步 E_FAIL + 零回包意味着
请求在 SimConnect 客户端/传输层就被拒，MSFS 没当异步任务处理——这不是
SDK 文档记载的正常拒绝路径（正常应回包 CREATE_OBJECT_FAILED=22）。

对照 GitHub 同类问题（odwdinc/Python-SimConnect#112/#113、msfs-rs、
node-simconnect）后，剩余候选根因按概率排序：

1. **停在主菜单/未进世界**（最大嫌疑）：AI 创建要求已加载的场景。
2. **机模 title 不在装机列表**："Airbus A320 Neo Asobo" 是 2020/2024 默认
   机，但改装/精简安装或 2024 新版内容系统下容器名可能不同。
3. MSFS 2024 新 AI 体系对该族 API 的限制（版本相关，可能性较低）。

**已把 spike 升级为诊断工具（`probe` 模式）**：
- 打印 OPEN 回包：模拟器名 + 应用版本 + SimConnect 版本（区分
  MSFS2020/2024/P3D）；
- 请求用户机位置（顺带验证数据通道是否正常）；
- 对 AICreateNonATCAircraft / AICreateParkedATCAircraft /
  AICreateSimulatedObject × 空title/A320neo/747-8f/C172 共 12 种组合
  逐一报 HRESULT + 等回包，成功的立即清理；
- 汇总判定：全 E_FAIL 零回包 → 坐实"未进世界"；某 title 成功 → title
  问题；ParkedATC 成功而 NonATC 失败 → 注入器改用按机场创建（接口不变，
  仅换 DLL 调用）。

**下一轮请跑**（MSFS 加载进机场后再执行）：
```
py tests\spikes\spike_owned_ai.py probe
```
把整个输出誊抄回本小节。若 probe 给出可用组合，P1 注入器按结论调整
（title 默认值或 API 选择），再继续 spawn/plan/drive 三项验证。

#### P0 第四轮实测（2026-09-22 probe 矩阵，突破性进展）

probe 输出（15 组合）核心数据：

| 现象 | 结论 |
|---|---|
| 全部调用 **HRESULT=0（S_OK）** | API 链路已完全打通（前一问的 E_FAIL 是未进世界导致） |
| ParkedATC 全部 **OBJECT_OUTSIDE_REALITY_BUBBLE(33)** | 创建点在用户现实气泡外（默认坐标 KJFK，用户实际在欧洲航线） |
| NonATC/SimObj 全部 **CREATE_OBJECT_FAILED(22)** | 大概率同因：坐标远离已加载区域 |
| 用户位置探测 **DUPLICATE_ID ×3** | spike 的 AddToDataDefinition datumID 传了 0（我方的 bug） |

根因与修复：

1. **现实气泡约束**（重要事实，直接影响 P1 设计）：AI 创建点必须在用户
   已加载区域内。→ spike 已改为先读用户机位置，创建点默认落**用户附近
   （北侧约 2NM）**；这恰好就是本计划的正确场景（玩家在离场机场，自有
   AI 生成在玩家身边）。P1 注入器的 spawn spec 由调用方传现场坐标，
   设计无需改动。
2. **spike datumID bug**：AddToDataDefinition 第 7 参传 0 → 每次
   DUPLICATE_ID。已改传 SIMCONNECT_UNUSED(0xFFFFFFFF)，与
   simconnect_traffic.py 生产代码一致。用户位置/title 探测恢复可用。
3. **发现第二个库版本陷阱**（生产相关，已修）：用户装的是新版
   python-SimConnect（≥0.4.9，argtypes 已带库类检查），其 `connect()` 会
   起**后台 timerThread 持续泵 my_dispatch_proc_rd**，与我们的自建泵
   竞争同一消息队列——probe 里 EXCEPTION 全被库线程打印走、我们的表里
   "无回包"（误导性汇总的来源）。若生产注入器不处理这个竞争，
   ASSIGNED_OBJECT_ID 可能被库线程赢走（库只塞环境变量），spawn 必超时。
   → 已实现 `_install_dispatch_chain`：包装库的 my_dispatch_proc 并用库
   类型重建 my_dispatch_proc_rd，**两条投递路径都先过我们的处理器**，
   再走库默认处理。spike 同样处理。回归测试
   `test_dispatch_chain_covers_library_background_pump` 固化。

**下一轮请跑**（MSFS 在世界中，直接跑 spawn 即可——现在创建点自动
落在你附近，无需再设 OF_LAT/OF_LON）：
```
py tests\spikes\spike_owned_ai.py spawn
py tests\spikes\spike_owned_ai.py plan  D:\1.pln
py tests\spikes\spike_owned_ai.py drive
```
预期：spawn 拿到 objectID 并在你附近看到一架 A320neo（P0-1 达成）；
plan 模式看它是否自主滑行（P0-2，A 路径）；drive 模式看逐帧写地速是否
即时生效（P0-3，B 路径）。若 ParkedATC 仍要试，`set OF_AIRPORT=你当前
机场ICAO` 后再跑 probe。

#### P0 第五轮实测（2026-09-22 probe 第二轮，结构体修正+发包关联）

前两轮修复全部生效（datumID 修好后用户位置/title 读到了；分发链修好后
`[recv]` 与库打印双路都出现，`_EXCEPTIONS` 表 15 条全满）。但暴露**两处
结构体定义错误**（本地定义 vs 库/SDK 真实布局）：

1. `SIMCONNECT_RECV_EXCEPTION`：头 3 DWORD 后是 **5 个** DWORD
   （dwException/UNKNOWN_SENDID/dwSendID/UNKNOWN_INDEX/dwIndex），我只
   定义了 3 个 → 读出的"dwSendID"其实是发包 id（矩阵 sendID=11-25 的
   来源），且异常永远关联不上在途 spawn（每行 exc=None 的来源）。
2. `SIMCONNECT_RECV_SIMOBJECT_DATA`：dwData 前还有
   dwentrynumber/dwoutof/dwDefineCount 三个 DWORD → dwData 整体偏移
   12 字节，float64 全解错（lat=0/lon=3e118 的位置垃圾根因）。旁证：
   错位 12 字节后的残片给出 lon 高位 0x40573D16 ≈ **94.6°E**——
   SimConnect 数据本身正确，纯粹是解析偏移。

**另一关键事实（P3 设计约束）**：EXCEPTION 回包按**发送包 id**
（`SimConnect_GetLastSentPacketID`）关联请求，**不是 request id**——
库的 RequestList.py/SimConnect.py 同样用它。注入器已改为每次
AICreateNonATCAircraft 后抓发包 id；异常先按发包 id 匹配，退回
request id（旧版 SimConnect 语义）。

**位置结论**：用户实际在**中国西部 ~94.6°E**（A350-900）；默认 KJFK
创建点本来就必失败。创建点自动读用户位置已工作，但因结构体 bug 第
四轮传的是垃圾坐标，CREATE_OBJECT_FAILED(22) 是必然结果，不能据此判定
API 不可用。

修复已进 `core/owned_traffic_injector.py` + spike；回归测试 +2
（发包 id 关联 / request id 回退两条路径；owned 用例现 38 个）。

**重跑**（同一场景，直接 spawn）：
```
py tests\spikes\spike_owned_ai.py spawn
```
预期：合法用户位置打印（~94.6E, ~40N 量级）→ 创建点=用户附近 →
ASSIGNED_OBJECT_ID 回包 → objectID 数字。拿到即 P0-1 达成，接着
plan / drive。ParkedATC 需要 `set OF_AIRPORT=你当前机场ICAO`。

#### P0 第六轮实测（2026-09-22 spawn，位置修好但 title 存疑）

结构体修好后数据全干净：**用户位置 lat=51.474 lon=-0.464 alt=95ft =
伦敦希思罗（EGLL）**（此前的 94.6°E 确认是错位 12 字节的解析残片），
title='A350-900 (Default Cabin)'，创建点=用户北侧 1.8 海里（气泡内），
异常已能按发包 id 正确关联（sendID(packet)=11）。

但 spawn 仍 **EXCEPTION 22 CREATE_OBJECT_FAILED**，位置假设已排除
（1.8 海里内必在气泡中）。剩余嫌疑：

1. **容器 title 不匹配（当前最大嫌疑）**：`TITLE` simvar 返回的是
   **涂装名**（A350-900 (Default Cabin)），`AICreateNonATCAircraft` 要的
   是**容器名**（如 Airbus A350-900 Asobo），两者不一定相同。默认试的
   "Airbus A320 Neo Asobo" 是否在装机列表也未验证（MSFS2024 内容系统
   变化可能改了默认机容器名）。
2. SimConnect 版本相关差异（库自带 0.4.x DLL）。
3. FSLTL 占满 AI 对象池（但那样应报 TOO_MANY_OBJECTS(11)，未见）。

probe 已升级 v2：title 候选扩到 ~12 个（用户涂装名 + 多种容器名变体 +
Asobo 默认机 + OF_TITLE 覆盖）；NonATC 分**地面/空中**两轮放置；
ParkedATC/SimObj 保留；每轮等待压到 1.2s（全程 ~2 分钟）。

**请依次跑**（在 MSFS 中、世界中）：
```
set OF_TITLE=A350-900 (Default Cabin)
py tests\spikes\spike_owned_ai.py spawn
set OF_TITLE=
set OF_AIRPORT=EGLL
py tests\spikes\spike_owned_ai.py probe
```
同时告知：你跑的是 **MSFS 2020 还是 2024**（影响默认机容器名判断）。
若某 title 成功 → P1 注入器改用该 title 为默认值；若 NonATC 空中成功
而地面失败 → 注入器按"空中生成"设计进场路径。

#### P0 第七轮实测（2026-09-22，P0-1 达成 ✅）

**用户环境 = MSFS 2024。`OF_TITLE=A350-900 (Default Cabin)` 下 spawn
完全成功**：

```
AICreateNonATCAircraft title=b'A350-900 (Default Cabin)' tail=OF001 req=9901
   创建点 lat=51.50438 lon=-0.46414（用户附近）
   返回 HRESULT=0
   [recv] ASSIGNED_OBJECT_ID req=9901 obj=115113998
AIRemoveObject obj=115113998 …（无异常）
```

结论与影响：

1. **P0-1 ✓**（创建并拿到有效 objectID）；**P0-4 基本 ✓**（移除调用无
   异常；目视无残留待确认）。
2. **根因确认 = title**：MSFS 2024 装机没有 "Airbus A320 Neo Asobo"
   容器（§6 的"空=默认 AI 机"路线在该装机上不可用）；用户当前飞机的
   title（涂装式容器名 `A350-900 (Default Cabin)`）可用。
   → `traffic.owned.default_model_title` 在该机器必须显式填；
     计划 §6 "空=默认 AI 机" 对 MSFS2024 部分装机不成立。
3. 位置（用户附近 1.8 海里）、发包 id 关联、dispatch 双路、argtypes
   全部经实机验证。

**待验证**：P0-2（A 路径 plan）/ P0-3（B 路径 drive）——带 OF_TITLE
重跑（cmd 已在 tests\spikes 下就别再加目录前缀）：

```
set OF_TITLE=A350-900 (Default Cabin)
py spike_owned_ai.py plan  D:\1.pln
py spike_owned_ai.py drive
```

probe v2 矩阵仍建议跑一次：确认"空 title / 各 Asobo 默认机"在
MSFS2024 是否全不可用、空中放置是否可行，为 P1 默认值提供依据。

#### P0 第八轮实测（2026-09-22，plan/drive/probe v2 矩阵）

**plan（A 路径，P0-2）**：spawn✓、`AISetAircraftFlightPlan` 返回 S_OK、
无 LOAD_FLIGHTPLAN_FAILED 回包——计划载入被接受。**飞机是否自主滑行/
起飞仍需目视确认**（P0-2 未结案）。

**drive（B 路径，P0-3）**：`AIReleaseControl` 正常；但逐帧
`SetDataOnSimObject` **全部 DATA_ERROR(20)**。根因：写的是
`GROUND VELOCITY`——计算类 simvar 不可写。spike 已改为写
PLANE LATITUDE/LONGITUDE/ALTITUDE/HEADING（可写 simvar）再验。

**probe v2 矩阵（12 title × 4 API）决定性结论**（MSFS 2024、用户机
A350-900 (Default Cabin)）：

| title | NonATC地面 | NonATC空中 | ParkedATC | SimObj |
|---|---|---|---|---|
| **A350-900 (Default Cabin)** | ✓ | ✓ | ✓ | ✓ |
| 空 title（默认AI机） | 22 | 22 | 34 | 22 |
| 各 Asobo 默认机（A320neo/737/747/C172/C152/A350-900 Asobo…） | 22 | 22 | 34 | 22 |

即：**该装机唯一可用容器 = 用户当前飞机**（全部 4 个创建 API 通用）；
"空=默认 AI 机"（计划 §6）与所有 Asobo 默认机在本装机不存在。

**工程落地（已完成）**：`OwnedTrafficInjector.spawn` 增加 **title 兜底**——
创建被 22/34 拒绝时，自动查用户当前飞机 TITLE（SIMOBJECT_DATA 通道，
会话缓存）并重试一次。`traffic.owned.default_model_title` 仍为首选，
只是 MSFS2024 上大概率走到兜底。回归 +4（兜底成功/无 title 不兜底/
datumID=UNUSED/超时不兜底），owned 用例 42。

**重跑 drive 验证 B 路径**（spike 已改可写 simvar）：
```
set OF_TITLE=A350-900 (Default Cabin)
py spike_owned_ai.py drive
```
预期：11 步全部 OK（无 DATA_ERROR），且 MSFS 里飞机沿 310° 移动。
**plan 目视**：重跑 plan 并在接下来 60 秒里盯着那架 A350 是否自行
滑行/起动/起飞——这是 P0-2 的最终判据。

#### 附：看不到生成机的排查（P0-9，2026-09-22，已结案）

用户实测“创建成功（拿到 objectID）但 MSFS 里看不到飞机”。逐项排除：

1. **不是 FSLTL 占满 AI 配额**：若对象池满，MSFS 会回
   `SIMCONNECT_EXCEPTION_TOO_MANY_OBJECTS(11)` 拒绝创建；三轮实测从未
   收到 11，每次都是 S_OK + 有效 objectID——sim 已接受并创建对象。
2. **位置太远**：默认创建点在用户正北 1.8 海里，座舱视野外。spike 已加
   `OF_OFFSET_NM`（默认 1.8，设 0.3 ≈ 550m 可“贴脸生成”），并在拿到
   objectID 后打印“切外部视角朝北看”的明确指引。
3. **观察窗口太短**：spawn 模式原来 10 秒就移除；已改为 60 秒
   （`OF_WATCH_SECONDS` 可调）并每 5 秒倒计时提示。
4. **MSFS 图形设置**：选项→图形→交通→"AI 飞机密度"若为 0，AI 飞机
   （含本方案自建机与 FSLTL 交通）一律不渲染——对象存在但不可见。
   （注：该滑条是 MSFS2020 的；**MSFS2024 已取消此设置**，SimConnect
   创建的 AI 对象不受密度滑条控制。）
5. 若以上都排除仍看不到：用 plan/drive 模式观察（那两模式会动），或在
   probe 汇总里看 objectID 是否持续有效。

**P0-9 结案（2026-09-22）**：原因 = ① 默认创建点在正北 1.8 海里（座舱
视野外）+ ② 旧版 spawn 只存在 10 秒。**与 FSLTL/AI 配额无关**（配额满会
回 TOO_MANY_OBJECTS(11)，从未出现）。spike 加 `OF_OFFSET_NM`（0.3 海里
贴脸生成）与 60 秒观察窗后，用户实测**飞机清晰可见**——P0-1 完整闭环：
创建✓、objectID✓、可见✓、移除✓。

**P0 总结账（截至 2026-09-22）**：
- P0-1 ✓✓（创建+可见+移除，含 MSFS2024 title 约束的完整认识）
- P0-4 ✓（AIRemoveObject 无异常）
- P0-2 待目视：plan 模式 S_OK 已确认，内建 AI-ATC 是否自主滑行/起飞
  需用户在 60s 观察窗内确认
- P0-3 待复测：drive 模式旧版写 GROUND VELOCITY 全 DATA_ERROR；已改
  写 PLANE LATITUDE/LONGITUDE/ALTITUDE/HEADING，待用户复测

#### P0 第十轮实测（2026-09-22，A/B 两路径判定）

**plan（A 路径，旧构造）= 证伪**：`AICreateNonATCAircraft` 创建的飞机
**按定义不受内建 ATC 管辖**——`AISetAircraftFlightPlan` 返回 S_OK、无
异常，但 60 秒原地不动。非 ATC 机 + 飞行计划 ≠ 自主滑行。
→ spike 的 plan 模式已改走 **`AICreateParkedATCAircraft`**（停机位创建、
ATC 管辖；probe v2 已实测本机可用）+ 飞行计划。用法：
`set OF_AIRPORT=EGLL` + `py spike_owned_ai.py plan <从EGLL出发的.pln>`。
旧流程保留为 `plan_nonatc` 模式作对照。

**drive（B 路径）= 半通**：换写可写 simvar 后，11 步
`SetDataOnSimObject` **全部 S_OK（DATA_ERROR 消失，写入通道打通）**，
飞机肉眼可见地瞬移；但**随后被拉回原位置**。机理：`AIReleaseControl`
虽返回 S_OK，1Hz 的位置写入仍被 sim 的停机/物理状态恢复盖住——
单纯低频写位置无法驱动飞机。
→ 结论符合计划 §4 P3-B 的预判（"要自己写简化运动模型"）：B 路径需要
  高频全状态写入（位置+姿态+速度+on-ground，~10Hz+）才可能与地面物理
  拔河；作为"精确模式"后置，不作为 MVP 主路径。

**P0 总结账（更新）**：P0-1 ✓✓ / P0-4 ✓ / A 路径待复测（ParkedATC 新
构造）/ B 路径降级为后置精确模式。按计划 §10 决策点：**P3 主路径走 A**
（ParkedATC 或 EnrouteATC 创建 + 飞行计划 + 放行门控），B 模式待 P3-A
闭环后再评估高频全状态写入方案。

#### P0 第十一轮实测（2026-09-22，ParkedATC + 计划仍不动）

复测：`OF_AIRPORT=EGLL` + `spawn_parked_atc`（obj=144506909，ATC 管辖）
+ `AISetAircraftFlightPlan`（S_OK、无异常）→ **60 秒仍原地不动**。

两种解释待分辨：① MSFS 的 AI 离场是**排班制**（挂计划后要等放行调度/
推出/开车，60s 太短）；② MSFS2024 未把 SimConnect 自建机纳入 AI-ATC
调度（社区有类似报告）。**诊断已升级**：
- plan 模式观察窗 60s → **300s**（`OF_WATCH_SECONDS`），且每 30s 主动读
  该机 LAT/LON/ALT——以**位置遥测**而非目视判断是否动过，终态打印累计
  里程并给 ✅/❌ 结论；同时前置校验 .pln 存在性与 XML 格式。
- drive 模式升级为 **P3-B 真实形态**：默认 10Hz 全状态写入（位置+航向，
  `OF_STEP_HZ/OF_DRIVE_SECONDS/OF_DRIVE_NM` 可调），写完后**读回实际位置**
  验证终点是否守住——直接回答"高频写能否战胜模拟器恢复"。

**P0 总结账（再更新）**：P0-1 ✓✓ / P0-4 ✓；A 路径机器链路全通但自主性
待 300s 遥测判定；B 路径 1Hz 被证伪、10Hz 待判定。若 A 的 300s 遥测仍
❌ 且 B 的 10Hz 也 ❌，则按计划 §8 走"退而求其次只做表现层增强"的分支
决策（保留 owned 注入器的创建/去重/回灌，把"指挥起飞"降级为视觉表现）。

#### P0 第十二轮实测（2026-09-22，A 路径成立，P0 结案 ✅）

`AICreateParkedATCAircraft(EGLL) + AISetAircraftFlightPlan` + 300s 位置
遥测：

```
[32s/62s/93s] 未动（推出/开机/等滑行许可——用户同时目视确认后推+滑行）
[123s] 0.03NM → [183s] 0.09NM → [244s] 0.22NM → 终态 0.35NM ✅
```

**P0 结案结论**：
- **P0-1 ✓✓** NonATC/ParkedATC/SimObj 三 API 创建均可用，唯一可用
  title=用户当前飞机（MSFS2024 装机无 Asobo 默认机容器），可见、可移除。
- **P0-2 ✓** `ParkedATC + 飞行计划` → 内建 AI-ATC 自主推出/滑行（约
  90-120s 排班延迟后启动，之后持续移动）。**A 路径可行。**
- **P0-4 ✓** AIRemoveObject 干净移除。
- **P0-3 部分**：SetDataOnSimObject 写可写 simvar 全部 S_OK、肉眼可见
  瞬移；但 1Hz 会被 sim 拉回。B 路径按计划预判降级为后置精确模式
  （10Hz 高频全状态写入未测也不必测——A 路径已成立，先打通闭环）。
- 附带事实：D:\2.pln 非 XML 格式（警告了），AI 依然动了起来——计划被
  接受即触发调度；正式实现时用 MSFS 保存的标准 .pln。

**对 P3 的设计输入（重要）**：
1. 注入器的创建默认从 `AICreateNonATCAircraft` 改为
   `AICreateParkedATCAircraft(airport_icao)`——需要机场 ICAO，由
   logic_manager 的当前机场上下文提供（spec 增 `airport` 字段）。
2. **放行门控 = 飞行计划的赋予时机**：不带计划 → 飞机永停（P0-1 已证）；
   带上计划 → ~90-120s 后自主滑行。因此 P3-A 的"指挥"实现为：
   `sequencer 排到可放行 → emit owned_traffic_cleared → controller
   AISetAircraftFlightPlan(起飞计划)`，天然满足"放行前原地等"。
3. 时序约束：从赋予计划到实际滑跑约 90-120s（AI 排班延迟），spawn 时机
   应提前到玩家开始滑行/排队时，由 controller 按 sequencer 队列深度决定。
4. despawn 时机：`despawn_on_airborne_nm`（§5 配置）——离场 N NM 后
   AIRemoveObject 交还"想象空间"。



#### 实施状态（2026-09-22，离线环境 WSL）

**P0 仍未验证**（本环境无 MSFS，§9 清单保持未勾）。剩余待办（仅能在
Windows + 真实 MSFS + FSLTL 下执行，任一步不过即回到本步）：

```
python tests/spikes/spike_owned_ai.py caps    # 先跑：DLL 能力探测
python tests/spikes/spike_owned_ai.py spawn   # 创建一架并核对 §9 P0-1
python tests/spikes/spike_owned_ai.py plan <PLN>   # A 路径（P0-2）
python tests/spikes/spike_owned_ai.py drive        # B 路径（P0-3）
```
跑完把每个 API 的实测签名/返回/坑誊抄进上一节。

**已离线落地（不依赖 P0 结论，均在 `traffic.owned.enabled` 开关后，默认关）：**

- [x] **P1** `core/owned_traffic_injector.py`：`OwnedTrafficInjector`
  （独立 SimConnect 实例 + 自建 dispatch 线程 + ctypes 自有 dispatch proc；
  `spawn/despawn/despawn_all`；呼号去重避让 FSLTL；`max_aircraft` 限流；
  DLL 异常 / 回包 EXCEPTION / 超时三路失败降级互不影响；pending 期间
  despawn 的迟到回包会补 `AIRemoveObject` 防孤儿 SimObject）。共用的
  `AICreateNonATCAircraft` / `AIRemoveObject` 是 A/B 两路径共同依赖，
  与 P0 的路径选择无关。
- [x] **P2** `core/traffic_manager.py`：`AircraftTrackingData` 增
  `owned` / `owned_id`；命中注入器 `object_id` 索引的枚举机自动打
  `owned=True`；`traffic_update` bulk 列表透出两字段。sequencer /
  chatter 零改动纳入自有 AI。
- [x] **接线**：`config.example.json` / `config.json` 的 `traffic.owned`
  段（见 §5，`enabled` 默认 `false`）；`app.py` 在开关打开且
  provider 为 msfs/p3d/fsx 时创建注入器并挂接交通表；去重源接交通表呼号。
- [x] **测试**：`tests/test_owned_traffic_injector.py` 28 例（假 DLL 全
  链路 + P2 集成）；`tests` 全量 78 例通过（5 例 loader 失败系离线机缺
  networkx 依赖，与本次改动无关）。

**仍需实机的项**：P0 全部；P1/P2/P3 的验收项（§9 清单未勾部分）；
[需实机验证] MSFS 由尾号推导 ATC ID 的规则（决定 `owned` 标记命中率）。

**P3-A 已落地（2026-09-22，随 P0-12 结论）**：
- `core/owned_traffic_injector.py`：spawn 支持 `spec.airport` →
  `AICreateParkedATCAircraft`（A 路径）；`set_flight_plan(owned_id, pln)`
  （放行动作）；`request_owned_position` + SIMOBJECT_DATA 位置回包路由。
- `core/owned_traffic_controller.py`（新）：监听 `owned_traffic_cleared`
  → 赋予飞行计划（**放行门控 = 计划赋予时机**：不带计划永停，P0-1 已证）；
  1Hz tick 按 `despawn_on_airborne_nm` 自动回收并 emit
  `owned_traffic_departed`。
- `app.py`：controller 随注入器启动；三个手动验证路由
  `POST /api/owned_traffic/spawn|clear`、`GET /api/owned_traffic`。
- 测试：owned 注入器 47 例 + 控制器 16 例（放行/回收/生命周期/降级），
  全量 113 例通过（5 例 loader 失败系离线机缺 networkx，与本次无关）。

**P3 自动策略已落地（2026-09-22）**：
- `core/owned_traffic_policy.py`（新，纯决策可单测）：`on_takeoff_request`
  输出 spawn/release 动作——① 活跃自有 AI < max 且已知机场 → 早生成
  （消化 AI-ATC 排班延迟）；② `release_interval_s` 冷却满足 → FIFO 放行
  最年长的未放行机。呼号池自动避让现有交通。
- `core/logic_manager.py`：`_owned_traffic_policy_tick()` 挂在 Tier-0
  起飞请求路径（独立于 sequencer 开关）；release → emit
  `owned_traffic_cleared`（controller 执行）。策略异常不影响 ATC 主链。
- 配置增补 `owned.flight_plan` / `owned.release_interval_s`（见 §5）。
- 测试：策略 15 例（生成/放行/冷却/呼号避让/failed 忽略），全量 128 例
  （5 例 loader 失败系离线机缺 networkx，与本次无关）。

**下一步（待办）**：实机闭环验证（spawn→停留→clear→~2min 自主滑行→
5NM 回收）后，按队列遥测细化放行时机（尾流间隔对齐、spawn 提前量）。

**P4 已落地（2026-09-22）**：
- **UI 角标**：`plugins/community/departure_queue_panel` 的
  `traffic_update` 行内渲染"🎯可指挥"badge（P2 回灌的 owned/owned_id
  字段），面板标题行附"· 🎯可指挥 N 架"汇总；FSLTL 只读机无标记不渲染。
- **清理钩子**（计划 §8 孤儿 SimObject 风险）：
  · 注入器 dispatch 线程遇 OSError（sim 退出/断链）→
    `_on_connection_lost()`：标记不可用、清 object_index/pos 在途、
    全部 owned 记 failed、emit `owned_traffic_unavailable`；
  · app.py `atexit` → controller.stop() + traffic_manager.stop() +
    injector.stop()（内部先 despawn_all）。注：atexit 不覆盖
    taskkill/断电，那种情况靠 sim 退出自毁对象。
- **已知边界（后续增量）**：断连后不自动重连（需重启 app 或显式
  restart 注入器）；场景切换（flight load）若存留连接，旧 object 失效
  由 despawn_on_airborne_nm 兜底回收，更细的 SimStart 事件联动待排。


---

## 10. 落地顺序小结

1. **P0 spike** —— 唯一的"去/不去"决策点，必须最先在实机做。
2. P1 注入器骨架（创建/销毁/去重）。
3. P2 状态回灌（让 sequencer/chatter 无感知地纳入自有 AI）。
4. P3 起飞驱动（先 A 路径打通闭环，B 精确模式后置）。
5. P4 配置/UI/清理/打磨。

每一步都在 `owned.enabled` 开关后面，随时可整体回退到纯 FSLTL 只读。