# OpenFrequency 全量升级计划 v2：签派→停机全流程 ATC + 游戏内数据 + FSLTL 联动

## 0. 本文档的定位

本文件是对 `docs/cuddly-wiggling-valley.md` 的**勘误版 + 施工版**。原计划的方向（阶段机复用、源优先级链、SimConnect AI 枚举、DepartureSequencer）经逐行核实后成立，但其中 11 处事实陈述与代码或外部权威资料不符、6 处关键机制未覆盖，按原文实施会出现"改完但不工作"或"跑偏"。

本文所有结论都对照当前工作区源码核实到行号，引用格式 `文件:行号`；涉及外部规范的部分在 §11 给出可核查来源。新会话应按本文执行，不要再回读原计划作为事实来源。

工程基线：`git log` HEAD = `41215ef`，工作树新增 `docs/cuddly-wiggling-valley.md` 与本文档（均未跟踪），无其他未提交改动。所有改动从该基线开分支。

---

## 1. 原计划勘误（必须先修正的认知）

| # | 原计划陈述 | 代码事实 | 修正动作 |
|---|---|---|---|
| E1 | `sid/star/approach` 已在 `STICKY_FIELDS` | `core/atc_session.py:165` 只有 `{callsign, squawk, runway, arrival_runway, sid, cruise_alt}`。`star` 不存在；进近字段叫 `approach_clearance` 且不在该集合 | **仅把 `star` 加入 `STICKY_FIELDS`**（SID/STAR 一经放行不再变）；`approach_clearance` **只加入 `PROMPT_FIELDS`（:345-358），不要设 sticky**——进近许可会合法变更（改跑道、复飞后重新指定），设 sticky 会导致第二次进近许可被 `assign`（:541）静默拒绝。B1 的 `star` 写入才有落点，`approach_clearance` 保持可覆盖 |
| E2 | 复用 `SimConnectProvider.sc` 句柄即可枚举 AI | `SimConnect.py:197`（python-SimConnect 0.4.8）的 `request_data()` 把 `RequestDataOnSimObjectType` 的 type/radius **写死为 `SIMCONNECT_SIMOBJECT_TYPE_USER` 和 0**，`AircraftRequests` 通道永远只读用户机 | 必须自建 data definition + request id 并自收 `SIMCONNECT_RECV_ID_SIMOBJECT_DATA_BYTYPE`，见 §6.C1 |
| E3 | AI 对象可读 `CATEGORY` 得到机型/尾流 | `CATEGORY` 不是 AI SimObject 上的 SimVar。可用的是 `ATC ID`、`ATC TYPE`、`ATC MODEL`；wake 需由 `ATC TYPE` 映射 | 改字段清单；`SimConnect/RequestList.py:1082` 的 `__AIControlledAircraft` 已含 `AI_TRAFFIC_*` 系列，优先用（见 §7.D1） |
| E4 | SimBrief OFP 有 `origin.plan_rwy/sid/star` | 真实字段是 `origin.sid_ident`、`destination.star_ident`、`origin.plan_rwy`、`destination.plan_rwy`；且 SimBrief 会把 SID/STAR 标识**截断 1 个字符**（`OBOKA4G → OBOK4G`，SimBrief 官方确认行为） | 按正确字段名解析；ident 匹配本地库时做 1 字符截断容错 |
| E5 | `_determine_controller` 频段过窄"导致放行常被误判为 Tower" | `core/logic_manager.py:517-538` 主路径是 `_find_frequency_entry` ±0.01 匹配频率库，只有匹配失败才走 `:530` 兜底；且 121.6–121.95 是 Ground 窗，中国常见 CD（如 ZGGG 121.95）实际被判成 **Ground**，不是 Tower | 改写描述：CD 不能用频段法推断，只能按机场库反查；修频段表时删掉 `118.95<f<119.0` 这个几乎见不到的假窗 |
| E6 | 已有 `navdata.frequency_source` / `navdata.ground_source` | 代码与 `templates/settings.html:801-802` 确实读这两个键，但 `config.example.json` 顶层只有 `user_profile/connection/audio/simbrief/immersion/cloud/ui`——**`navdata`、`simulator`、`traffic` 三整个段落在示例配置里缺失**，`navdata.sqlite_path` 连设置页入口都没有 | 阶段五必须补全 `config.example.json` 三段 + 设置页入口，否则 B1 的导航库查询永远拿到空路径 |
| E7 | B1 写"查询 `approach`/`transition`/`approach_leg` 表得到真实程序" | LittleNavMap 把 **SID/STAR/进近统一存在 `approach` 一张表**，用 `type` 列区分（`D`=SID、`A`=STAR、其余为进近类型），过渡在 `transition` 表；没有独立的 sid/star 表。列名为 `airport_ident`（ICAO 字符串）而非 `airport_id` | 按 §5.B1 的真实 schema 改写查询 |
| E8 | B1 写 X-Plane CIFP 位于 `Custom Data/CIFP/<ICAO>.dat` | X-Plane 的 CIFP 是**单个全量文件** `<X-Plane>/Custom Data/earth_424.dat`（由 FAA CIFP 重命名放入），不是 per-ICAO 文件；且只覆盖美国 | 改为探测 `earth_424.dat` + 全量流式解析按 ICAO 过滤，见 §5.B1 |
| E9 | D1 尾流间隔"Light 60s / Medium 90s / Heavy 120s / Super 157s" | 该数值无出处。ICAO Doc 4444 (Amd.9, 2020) 的真实间隔见 §7.D1 表：**起降时间型间隔为 2–4 分钟**，最短 2 分钟；起飞间隔还取决于是否用全跑道 | 替换为 ICAO 权威表，见 §7.D1 |
| E10 | C1 "半径取用户可配（如 40 NM）" | MSFS 官方文档明确 `dwRadiusMeters` 上限 **200,000 m（≈108 NM）**，超出返回 `SIMCONNECT_EXCEPTION_OUT_OF_BOUNDS`；设为 0 时只返回用户机 | 配置需 clamp 到 ≤200 km，并说明 0 的语义 |
| E11 | 隐含假设"FSLTL = 实时 live 交通" | FSLTL 的 FR24 免费 API 已于 **2026-04-30 关闭**，injector 一度退化为纯静态时刻表注入（v1.9.0）；2026-05-26 Navigraph 与 FR24 合作后新版才恢复 live 注入 | 交通源按三态设计（live/静态/未运行），见 §6.C2 与 §12 风险表 |
| E12 | 把 `DISPATCH` 设为**所有航班**的强制初态、`DISPATCH→ATIS` 靠 PDC 复诵置位的 `fpl_confirmed` 推进 | 代码里 `flight_rules` 是一等概念（`app.py:1903-1910` 可切 IFR/VFR，`core/llm_client.py:577` 有完整 VFR 分支）。VFR/通航航班**不申请 PDC**，`fpl_confirmed` 永不为真 → 开局即**卡死在 DISPATCH**。这与 G1 批评原计划的"漏同步"是同类错误 | `DISPATCH` 仅对 IFR 生效；VFR 初态与推进见 §4.A1「flight_rules 分流」 |
| E13 | `ROLE_FREQ_FALLBACKS["Dispatch"]=("Clearance Delivery",)` 让 DISPATCH 显示并复用 CD 频率即可 | `advance_to_role`（`atc_session.py:441-470`）按 role→phase 反查。DISPATCH 与 CLEARANCE 指向**同一频率**时，`observe_tuning` 无法区分该对齐到哪个阶段（两者都在 candidates 里，取 `forward[0]` 会命中 DISPATCH 而非 CLEARANCE），造成 tune 到 CD 频率反而回退/停在 DISPATCH | PDC 走数据链而非语音频率；DISPATCH **不参与 `advance_to_role` 的频率反查**，见 §4.A1「DISPATCH 触发机制」 |

以上 E7–E11 来自外部文档与 LittleNavMap 官方源码核实：MSFS SimConnect 官方文档、ICAO Doc 4444/Amd.9、[LittleNavMap `src/query/procedurequery.cpp`](https://github.com/albar965/littlenavmap/blob/master/src/query/procedurequery.cpp)、[devsupport "Missing SimVar in the Documentation"](https://devsupport.flightsimulator.com/t/missing-simvar-in-the-documentation/4364)、[Wake turbulence category (Wikipedia 汇总 ICAO Doc 4444)](https://en.wikipedia.org/wiki/Wake_turbulence_category)、[FSLTL 停服报道](https://fsnews.eu/fsltl-impacted-by-flightradar24-api-shutdown/)、[simdispatch 工具现状（含 Navigraph 恢复 live）](https://simdispatch.de/2026/06/10/best-free-tools-msfs-2020-2024/)、[X-Plane earth_424.dat 放置方式](https://github.com/TripleJumpStudios/CIFP-Updater)。

---

## 2. 结构性缺口（不补则功能空转）

| # | 缺口 | 后果 | 补法 |
|---|---|---|---|
| G1 | 插 `DISPATCH` 后未同步 4 处旁支 | 初态仍是 `"ATIS"`（`atc_session.py:324`）→ 开局即跳过签派；`atc_handoff.ATCPhase` 枚举（`atc_handoff.py:24-35`）ValueError 回退 ATIS；`ROLE_LABEL_ZH` 无 `Dispatch` 键 → 中文 prompt/前端显示英文；`ROLE_FREQ_FALLBACKS` 无 `Dispatch` → `contact_for` 无频率 → `dashboard.html:947` 按钮 `disabled` | 见 §4.A1 的"同步清单" |
| G2 | `ACTION_PREREQS` 是静态 tuple（`atc_session.py:112-116`） | 表达不了"停机位可直接滑出时例外"，跑道边/远距起动位用户会被 Tier-0 永久重定向去 Clearance，死循环 | 见 §4.A2 条件化 prereq |
| G3 | 进港滑行终点取不到图节点 | apt.dat 源的 `startup_locations` 只是元数据（`simulator_ground_service.py:433-447`），`taxi_router.py:36-40` 只把 `stand_name` 贴到最近 taxi_node，**stand 本身不是节点**；只有 OSM 源显式加了 `stand:*` 节点和边（`osm_ground_service.py:215-243`）。非 OSM 用户 `suggest_taxi_in_route` 无终点 | 见 §4.A3 的"stand 入图" |
| G4 | `atc_action` emit 位置选错 | 原计划挂在 `enforce_message`（`atc_session.py:1054`）。该函数是纯文本兜底，`on_llm_response` 第二参数 action 在四层 Tier 全传 `None`（`logic_manager.py:1000/1024/1047/1057`）。真正的结构化指令出口是 `_emit_instruction_cards`（:1216 定义，:1222 调用）产出的 `InstructionExtractor` 卡片 + Tier-0 `check_request` 返回 | 见 §7.D2 |
| G5 | holding point 判定与 AI 目标跑道有现成数据没用 | 需另写几何推算；实际 `_mark_hotspots`（`taxi_router.py:237-245`）已标记 runway 邻接节点（`runway_links>0`），MSFS 侧 `AI_TRAFFIC_ASSIGNED_RUNWAY` / `AI_TRAFFIC_CURRENT_ICAO` 是原生 SimVar | 见 §7.D1 |
| G6 | 验证节前提缺失 | 本机直接跑 `python -m unittest discover -s tests` 因缺 `networkx` 失败；且 `tests/test_atc_session.py` 有 4 组用例显式依赖当前 PHASES 顺序，改阶段梯必红 | 见 §9 验收与 §10 验证矩阵 |

---

## 3. 架构原则（沿用 + 增补）

- **复用阶段机**：新阶段/新指令一律注册进 `atc_session.py` 的 `PHASES` / `PHASE_ROLE` / `PHASE_AUTHORITY` / `ACTION_OWNER` / `ACTION_PREREQS`，让 Tier-0（`logic_manager.py:989-1025`）、`sequence_block` / `authority_block` / `state_block`、`enforce_message` 自动生效。不另建状态机。
- **源优先级链**：游戏内(sim 文件/SimConnect) → 本地导航库(LittleNavMap/CIFP) → SimBrief → 公开网络(OurAirports/OSM) → LLM。每类数据返回结构带 `source` 字段，沿用 `logic_manager.py:429` 已建立的约定。
- **降级优先于失败**：每个真实数据源失败都必须回落到现状行为（mock 交通 / OSM / LLM），由配置开关控制，不允许把"没数据"变成"没响应"。
- **单 session 实例不变**：`app.py:1809` 创建唯一 `ATCSession` 并注入 logic_manager / atc_monitor / atc_handoff。**不要**引入第二个 session 实例。
- **线程边界显式化**：任何与 SimConnect 的交互必须明确在哪个线程执行（见 §6.C1），不允许跨线程共享非线程安全实例。

---

## 4. 工作块 A：全流程阶段补全

### A1. 新增 `DISPATCH` 前置阶段

**阶段梯改动**（`core/atc_session.py:28`）：

```python
PHASES = [
    "DISPATCH", "ATIS", "CLEARANCE", "GROUND_DEP", "TOWER_DEP", "DEPARTURE",
    "CENTER", "APPROACH", "TOWER_ARR", "GROUND_ARR", "PARKED",
]
```

**同步清单（G1，漏一处就出事）**：

1. `PHASE_ROLE["DISPATCH"] = "Dispatch"`（:34-45）——保持独立角色键，**不要**复用 `"Clearance Delivery"`，否则 `advance_to_role`（:441-470）会把 CD 频率匹配到 DISPATCH 而非 CLEARANCE，阶段错乱。
2. `PHASE_LABEL_ZH["DISPATCH"]="签派"`、`PHASE_LABEL_EN["DISPATCH"]="Dispatch Dispatch"`；新增 `PHASE_LABEL_JA` 字典（:47-59 目前只有 ZH/EN，JA 要新建，下游 `sequence_for_ui()` :675-694 和 `dashboard.html:944` 同步加 `label_ja`）。
3. `ROLE_LABEL_ZH["Dispatch"] = "签派"`（:61-65）。
4. `ROLE_FREQ_FALLBACKS["Dispatch"] = ("Clearance Delivery",)`（:71-79）——DISPATCH 显示 CD 频率仅供 UI，保证 `contact_for` 有值、前端按钮可点。**但见下方「DISPATCH 触发机制」：DISPATCH 不参与频率反查推进（E13）**，文案在 `sequence_block`/enforce 里注明"PDC 经 ACARS(Hoppie) 数据链递交，非语音频率"。
5. `PHASE_AUTHORITY["DISPATCH"] = {"pdc", "flightplan_confirm"}`（:82-93）。
6. `_new_state()` 初态**按 flight_rules 分流**（见下）。新增 `"fpl_confirmed": False`。
7. `atc_handoff.ATCPhase` 增补 `DISPATCH = "DISPATCH"`（`atc_handoff.py:24-35`），否则 `_sync_mirror`（:99-104）落回 ATIS。
8. `observe_telemetry`（:498-528）新增首段：`phase == "DISPATCH" and self._state["fpl_confirmed"] → "ATIS"`。`fpl_confirmed` 由 PDC 复诵确认处置位（新方法 `confirm_flight_plan()`），与既有的 `mark_atis_copied`（:411-415）同模式。

**flight_rules 分流（E12，动工前必修，否则 VFR 死锁）**：

- `DISPATCH` 阶段仅对 **IFR** 有意义。session 需感知 flight_rules——`shared_context['flight_rules']`（`app.py:1910` 写入，默认 `'IFR'`）在 `session.adopt()` 时读入，或经 `_new_state()` 参数传入。
- **初态分流**：`_new_state()` 依 flight_rules 决定起点——`IFR → "DISPATCH"`，`VFR → "ATIS"`（VFR 不申请预放行）。因为 flight_rules 可能在建 session 后才由用户切换，`observe_telemetry` 首段追加保险：`phase == "DISPATCH" and flight_rules == 'VFR' → 直接推进到 "ATIS"`（不等 `fpl_confirmed`），避免用户开局设 IFR 后改 VFR 时卡死。
- **回归测试**：`tests/test_atc_session.py` 的 `SequenceTests`（:87）新增两组——IFR 初态为 DISPATCH 且 PDC 复诵后进 ATIS；VFR 初态为 ATIS 且永不停在 DISPATCH。

**DISPATCH 触发机制（E13）**：

- PDC（预放行）现实中经 **ACARS/CPDLC 数据链**递交，项目已有 `core/hoppie_acars.py` 与 `core/cpdlc_manager.py` 可复用，**不占用语音频率**。
- 因此 DISPATCH **不进入 `advance_to_role` 的 role→phase 频率反查**：在 `advance_to_role`（:441-470）计算 `candidates` 时排除 `PHASE_ROLE == "Dispatch"` 的项（或在 `NON_ATC_ROLES` 同款机制里把 Dispatch 加入"不可经调频进入"的集合）。这样飞行员 tune 到 CD 频率只会对齐到 CLEARANCE，消除同频歧义。
- DISPATCH→ATIS 的推进只由 `fpl_confirmed`（PDC 复诵/数据链 WILCO）或 VFR 分流触发，与调频解耦。

**意图与模板**：

- `_INTENT_PATTERNS`（:119-137）把 `("pdc", (r"申请.{0,4}预放行", r"请求.{0,4}预放行", r"request pdc", r"pdc", r"clearance on request"))` **插到列表最前**，避免被 `ifr_clearance` 的 `request clearance` / `申请放行` 抢先命中。
- `ACTION_OWNER["pdc"] = "DISPATCH"`，`ACTION_OWNER["flightplan_confirm"] = "DISPATCH"`。
- `atc_template_responder.py` 新增 `request_pdc` 模板（与既有 `respond()` 的 `intent + "Ground" in role` 同款结构，:46 附近）：回读 `shared_context['flight_plan']` 的 origin/destination/route/cruise_alt/route_waypoints，输出"预放行已批准"句式，SID 取 B1 解析结果而非 `route.split()[0]`。

### A2. 推出与开车细化

- session state 增加 `pushback_done`、`engines_started`、`pushback_ok`（站立机位可直接滑出时为 True）。
- **prereq 条件化（G2）**：`check_request` 的前置循环（`atc_session.py:764-771`）改为支持可调用项：

```python
ACTION_PREREQS = {
    "lineup": ("runway",),
    "takeoff": ("runway", "squawk"),
    "landing": ("runway",),
    # tuple 里可放 callable，接收 session，返回 True 表示前置未满足
    "taxi": (lambda s: not (s.get("pushback_done") or s.get("pushback_ok")),),
}
```

  循环体：`for prereq in ACTION_PREREQS.get(action, ()): missing = prereq(self) if callable(prereq) else not self.get(prereq)`。`pushback_ok` 的判定放在 `suggest_pushback_direction`：若 layout 的 startup_locations 中没有该停机位/该位置距最近 taxi_node 过长，则置 True。
- `pushback_done` / `engines_started` 由 Tier-0 的 `pushback` intent 分支落库（`logic_manager.py`  Tier-0 区，与 `runway_request` 的 :1027-1035 同款写法）。
- `taxi_router.py` 新增 `suggest_pushback_direction(stand)`：优先取停机位 `heading`（仅 apt.dat 源有，`simulator_ground_service.py:437`），OSM 源无朝向时退化为"选择唯一/最近的邻接滑行道边"（从 build_graph 时 `stand_name` 已挂上的节点取邻居边名）。`atc_template_responder.py:47` 的 `face west` 硬编码删除。

### A3. 到达侧补全

- `PHASE_AUTHORITY["GROUND_ARR"]` → `{"taxi", "gate_assignment", "taxi_to_gate"}`；`ACTION_OWNER` 增加 `gate_assignment` / `taxi_to_gate` → `GROUND_ARR`。
- `_INTENT_PATTERNS` 增加 `gate_request`（`申请停机位|请求廊桥|request gate`）与 `taxi_to_gate`（`滑行到停机位|taxi to (?:the )?(?:gate|stand)`）。注意顺序：`taxi_to_gate` 必须排在 `taxi`（:127）**之前**，否则"滑行到停机位"被 `taxi` 吞掉后进 GROUND_DEP 分支。
- **stand 入图（G3）**：`taxi_router.build_graph_for_airport` 内新增 `_attach_stands()`——遍历 `layout['startup_locations']`，为每个 `gate_id` 建 `stand:{gate_id}` 节点（不存在时），按 `osm_ground_service.py:215-243` 同款阈值（最近 taxi_node ≤120 m）连无向边。所有源统一走这条路，`suggest_taxi_in_route` 才有稳定终点。
- `taxi_router.suggest_taxi_in_route(airport_icao, aircraft_position, stand_ident)`：起点 = 距飞机最近的 runway 邻接节点（复用 `_mark_hotspots` 的 `runway_links>0` 标记），终点 = `stand:{stand_ident}`，走既有 `find_path`。返回结构与 `suggest_taxi_route` 对齐（path/taxiways/cost/runway_crossings）。
- 新增 `core/gate_assigner.py`：输入 layout 的 `startup_locations`（含 `operation`/`type`，如 Gates/Cargo/Ramp）、`aircraft_size`、可选 traffic 占用集合。占用判定：任一交通目标位于 stand 坐标半径 40 m 内且 `on_ground` 即为占用（数据来自 C2 的交通表，X-Plane/mock 无此数据时跳过占用检查）。输出 `{"stand": gate_id, "lat":…, "lon":…, "reason":…}`。
- `llm_client.py:491-517` 的 `ground_help`：条件从"填充离场路由"扩展为到达侧也注入 `assigned_gate` 与 `suggested_taxi_in_route`；`GROUND MOVEMENT RULES` 增加"当 assigned_gate 非 N/A 时，滑行指令必须终止于该 gate，且只能用 Taxiways known 列表内的滑行道名"。
- `PARKED` 阶段：`PHASE_AUTHORITY["PARKED"]` 保持空集，`PHASE_ROLE` 保持 `None`（:44/:92），`sequence_block` 的 `role is None → continue`（:827-828）继续跳过。到达服务播报（欢迎/关车/地面服务）作为纯文本包在 `enforce_message` 之后输出，`_fix_handoff_frequency:1035` 对 `PARKED`/`ATIS` 跳过注入的逻辑保持不变。

### A4. 修复既有 bug

1. `logic_manager.py:1090`：`phase = ctx_snapshot.get('flight', {}).get('phase', 'unknown')` —— `shared_context` 没有 `flight` 键（见 `core/context.py`），phase 恒为 unknown。改为 `self.atc_session.phase`（LogicManager 已持有实例）。
2. `logic_manager.py:621-647` `_determine_controller`：删掉 `118.95 < f < 119.0` 这条假 CD 窗。真实 CD 分布（中国多在 121.6–121.95，欧美多在 118–119）与 Ground/Tower 窗重叠，频段法不可靠。保留 Emergency/ATIS/Ground/Tower 三个可靠窗，其余返回 `None`，由调用方（:530）走 `_find_frequency_entry` 与 `ROLE_FREQ_FALLBACKS` 兜底。
3. `atc_handoff` 冗余判断修正：**不存在状态分裂**——`app.py:1809/1861` 把同一 session 注入两个管理器，双驱动 `observe_telemetry` 是幂等的。真正的重复是两份 `atc_phase_update` socket emit（`atc_handoff.py:137` 与 `logic_manager.py:889`）。处置：保留 atc_handoff（它承担 ATIS 自动获取与 `mandatory_handoff` 事件），删掉其中一份 socket 广播。

**阶段一改动文件**：`core/atc_session.py`、`core/atc_template_responder.py`、`core/taxi_router.py`、`core/llm_client.py`、`core/logic_manager.py`、`core/atc_handoff.py`、新增 `core/gate_assigner.py`、`tests/test_atc_session.py`。

---

## 5. 工作块 B：程序数据源（SID/STAR/进近）

### B1. 新增 `core/procedure_service.py`

对外接口：

```python
get_sids(icao, runway) -> list[dict]      # [{"ident": "PIKAS1D", "source": "lnm|cifp|simbrief|llm", ...}]
get_stars(icao, runway) -> list[dict]
get_approaches(icao, runway) -> list[dict]  # 含 type/suffix/ident
```

内部源链，任一步命中即返回并带 `source`：

1. **LittleNavMap SQLite**：路径取 `config['navdata']['sqlite_path']`（当前无 UI 入口，见 §8）。打开方式复用 `airport_frequency_service.py:400-439` 的既成模式（`os.path.exists` 守卫 + `sqlite3.connect` + 异常回空）。**真实 schema 已从 LNM 官方源码核实**（`src/query/procedurequery.cpp:2264-2345`、`:2521-2578`）：
   - SID/STAR/进近**共用 `approach` 表**，`type` 列区分：`'D'`=SID、`'A'`=STAR、其余（`GPS`/`ILS`/`VOR`…）为进近类型。
   - 关键列：`approach_id, type, arinc_name, airport_ident, suffix, has_gps_overlay, fix_ident, runway_name, runway_end_id`。关联键是 `airport_ident`（ICAO 字符串），**不是** `airport_id`。
   - 航路段：`approach_leg`（`approach_id`、`approach_leg_id`、`fix_ident`、`is_missed` 等），过渡段：`transition`（`transition_id`、`approach_id`、`fix_ident`）+ `transition_leg`（`transition_id`、`transition_leg_id`、`fix_ident`）。
   - 查询范式：`select approach_id, arinc_name, airport_ident, suffix, runway_name from approach where fix_ident like :name and type like :type and airport_ident = :icao`，随后 `select * from approach_leg where approach_id = :id order by approach_leg_id`。
   - **schema 随版本变动**（navdatareader 2025-11 给 `approach_leg`/`transition_leg` 加 `vertical_angle`，给 `waypoint`/`nav_search` 加 `arinc_type`）。实现一律 `select *` + 按需 `row.keys()` 取值，**不写死列清单**，缺列走 LLM 兜底。
   - db 文件形如 `little_navmap_msfs.sqlite` / `little_navmap_navigraph.sqlite`，位于 `%APPDATA%\ABarthel\little_navmap_db\`——把这个路径写进文档，用户才知道该填什么。
2. **X-Plane CIFP**：正确形态是 `<X-Plane>/Custom Data/earth_424.dat`——**单个全量 ARINC 424 文件**（官方/社区用法：把 FAA CIFP 下载后改名放入 `Custom Data`），不是 per-ICAO 文件。新增 `_discover_cifp_files()`：依次探测 `Custom Data/earth_424.dat` 与 `Resources/default data/earth_424.dat`，按 ICAO 前缀流式过滤记录，解析 SID/STAR/Approach 的 primary/continuation 行与 path terminator。注意该文件**只覆盖美国**，其他地区仍走 SimBrief/LLM。原有 `_discover_xplane_apt_files`（:110-140）只覆盖 apt.dat，不可直接复用。
3. **SimBrief**（`app.py:1283` 路由内）：解析 `data['origin']['sid_ident']`、`data['destination']['star_ident']`、`data['origin']['plan_rwy']`、`data['destination']['plan_rwy']`，连同既有 `navlog.fix` 一起写入 `_normalize_flight_plan`（`app.py:168-182`）产出 `sid`/`star`/`dep_rwy`/`arr_rwy` 键。ident 匹配本地库时实现 1 字符截断容错（SimBrief 已知行为，见 §1 E4）。
4. **LLM 兜底**：无上述数据时维持现状，由 LLM 依据 route 生成，显式标注 `source="llm"`。

**写入权威字段**：先完成 E1 修正（`STICKY_FIELDS` 增加 `star`；`PROMPT_FIELDS` 增加 `star` 与 `approach_clearance`，但 `approach_clearance` **不设 sticky**，保持可覆盖），再在 `logic_manager._update_issued_instructions`（:1290-1333）内把 `procedure_service` 的结果 `assign` 进 session。prompt 侧在 `llm_client.py` 的 fp_text（:373-386）旁加 `procedures` block，注明"有真实程序则逐字使用，否则可生成"。

### B2. 配置

- 新增 `navdata.procedure_source`，默认 `auto`（走源链），可选 `lnm` / `cifp` / `simbrief` / `llm` / `off`。
- `navdata.sqlite_path` 补 `templates/settings.html` 输入项（参照 :801-802 两行的读写模式），并在保存处（:900-901 附近）加入 payload。
- 文档明确 MSFS 编译 BGL 不可读是硬限制（`msfs_ground_service.py:1-7` docstring 已述），真实程序/滑行道依赖 LittleNavMap 或 dev-mode XML，否则回落 OSM+LLM。

**阶段二改动文件**：新增 `core/procedure_service.py`；改 `core/airport_frequency_service.py`（复用 sqlite 模式）、`core/simulator_ground_service.py`（CIFP 路径）、`app.py`（SimBrief 字段解析）、`config.example.json`、`templates/settings.html`。

---

## 6. 工作块 C：MSFS SimConnect AI 交通读取（FSLTL 数据基础）

> 本块依赖 python-SimConnect，实测 0.4.8 wheel 自带 `SimConnect.dll`（`SimConnect.py:9` 按包路径解析），因此"绑定具体 DLL 版本"的风险低于原计划估计；真实风险是 `requirements.txt` 未锁版本，见 §12 风险表。

### C1. 新增 `core/simconnect_traffic.py`

**数据定义（自建，不复用 `AircraftRequests`）**：

```
PLANE LATITUDE            float64  degrees
PLANE LONGITUDE           float64  degrees
PLANE ALTITUDE            float64  feet
GROUND VELOCITY           float64  knots
VERTICAL SPEED            float64  fpm
PLANE HEADING DEGREES TRUE float64 degrees
SIM ON GROUND             int32
ATC ID                    string256
ATC TYPE                  string256
ATC MODEL                 string256
AI TRAFFIC ASSIGNED RUNWAY string32
AI TRAFFIC ASSIGNED PARKING string32
AI TRAFFIC CURRENT ICAO    string32
AI TRAFFIC ISIFR          int32
AI TRAFFIC FROMAIRPORT    string32
AI TRAFFIC TOAIRPORT      string32
AI TRAFFIC ETA            float64
```

注：`AI TRAFFIC *` 系列在 MSFS 现行文档中缺失但**实测可用**（DevSupport 官方回复确认 `AI TRAFFIC ASSIGNED RUNWAY/PARKING/ETD/ETA/FROMAIRPORT/TOAIRPORT` 返回正确值）， python-SimConnect 0.4.8 的 `RequestList.py:1082` 也内置了这批 SimVar。实现时仍要做"读取失败→字段留空"的容错。

定义流程（每个字段一次 `AddToDataDefinition`，字符串字段用 `SIMCONNECT_DATATYPE_STRING256`）：

```python
dll = sm.dll
def_id = sm.new_def_id()
req_id = sm.new_request_id()
for name, unit, dtype in FIELD_TABLE:
    dll.AddToDataDefinition(sm.hSimConnect, def_id.value, name.encode(), unit.encode(), dtype)
# MSFS 上限 200,000 m；0 表示只回用户机（见 §1 E10），必须 clamp
radius_nm = min(float(cfg.get('navdata', {}).get('traffic_radius_nm', 40)), 100.0)
radius_m = max(int(radius_nm * 1852), 1)
dll.RequestDataOnSimObjectType(
    sm.hSimConnect, req_id.value, def_id.value, radius_m,
    SIMCONNECT_SIMOBJECT_TYPE.SIMCONNECT_SIMOBJECT_TYPE_AIRCRAFT,  # == 2
)
```

`RequestDataOnSimObjectType` 与 `SIMCONNECT_SIMOBJECT_TYPE_AIRCRAFT` 均已由 `Attributes.py:88-97` 与 `Enum.py:137-142` 绑定，直接取 `sm.dll.RequestDataOnSimObjectType`，**不要**再声明 ctypes 原型。

**接收与分发**：`SIMCONNECT_RECV_ID_SIMOBJECT_DATA_BYTYPE` 已由 `SimConnect.py:88` 分发到 `handle_simobject_event`。默认实现只按 `dwRequestID` 找 `self.Requests` 且把 `outData` 当作单定义取值，必须子类化 `SimConnect` 覆写 `handle_simobject_event`：按 `(dwRequestID, dwObjectID)` 双键路由，`dwData` 按 `len(definitions)` 长度 cast 成 double 数组逐字段取值，字符串字段单独处理（`cast(ObjData.dwData, c_char_p)`）。注意 `SIMCONNECT_RECV_SIMOBJECT_DATA_BYTYPE` 每帧只有**一个**对象，需累积成表。

**线程模型（硬约束）**：`get_data` 会在调用线程内跑 `CallDispatch`（`SimConnect.py:237-248`），SimConnect 实例非线程安全。traffic_manager 的 `_loop`（0.5 s）与 SimBridge 遥测线程若共享同一实例，会重入 `my_dispatch_proc`。二选一：

- **推荐**：`SimConnectTrafficReader` 自建独立 `SimConnect(auto_connect=True)` 实例 + 独立 dispatch 线程，与 `simconnect_provider` 的连接互不干扰（SimConnect 允许多 client 连接同一 sim）。
- 备选：全局 `threading.Lock` 串行化所有对该实例的调用，并把 dispatch 与遥测读取放进同一把锁。

### C2. 打通 `traffic_manager.py` MSFS 路径

- `_scan_traffic`（:248-289）：`hasattr(sm,'get_ai_aircraft_list')` 恒假 → 永远落 mock。改为调用 C1 的枚举结果，非空时喂 `update_aircraft`（:412）；仅当枚举失败或返回空且配置允许时，才 `_generate_enhanced_mock_traffic()`。
- **交通源三态（E11）**：FSLTL 的 FR24 免费 API 已于 2026-04-30 关闭，injector 曾退化为纯静态时刻表注入（v1.9.0），2026-05-26 起 Navigraph 授权的 FR24 数据恢复 live 注入。因此同一套 `simconnect_traffic` 必须同时服务三种现场：live 注入（AI 会真实滑行/排队）、static schedule（AI 按时表移动、密度低）、未运行注入器（零交通）。排队器在三种态下都要能给出合理结果（空队列时直接放行），且日志需标明当前检测到的交通源形态（依据：单位时间内新增/消失 AI 数量 + 是否存在 `AI TRAFFIC ETA` 非空值）。
- 复活 `_process_ai_object`（:325-340，当前唯一调用点在恒假分支内，实为死代码），改为接收 C1 结构化的 dict。
- `AircraftTrackingData`（:27-52）与 `update_aircraft` 增加 `aircraft_type`、`wake_category`、`assigned_runway`、`assigned_parking`、`icao_dest`。X-Plane 侧（`xplane_provider.py:360-438` 的 TCAS 通道）这些字段留空，`_scan_xplane_tcas`（:291-323）不传即可。
- `wake_category` 由 `ATC TYPE` 经 `core/aircraft_catalog.py` 或内置 ICAO 尾流表推导，拿不到时 `UNKNOWN`。

### C3. 能力探测与降级

启动时探测 `hasattr(sm.dll, 'RequestDataOnSimObjectType')` 与 MSFS 版本，失败则记录一次日志并把 `traffic.source` 标记为 `unavailable`，维持既有 mock 行为。探测结果写入 UI 状态栏（可复用插件 `set_status_bar_item`，或现有 sim status 通道）。

**阶段三改动文件**：新增 `core/simconnect_traffic.py`；改 `core/simconnect_provider.py`、`core/traffic_manager.py`、`requirements.txt`（锁 SimConnect 版本）。

---

## 7. 工作块 D：离场排队（DepartureSequencer）与队列接入

### D1. 新增 `core/departure_sequencer.py`（模拟器无关）

输入：

- `traffic_manager.aircraft`（含 C2 新字段）与 `TrafficState`（`traffic_manager.py:15-25`，`TAXIING`/`TAKEOFF_ROLL` 等已存在）。
- 本机 `atc_session.phase == "TOWER_DEP"` 与权威 `runway`。
- `TaxiRouter` 图的 runway 邻接节点集合（`runway_links > 0`，`taxi_router.py:237-245`）作为 holding point 判定，无需另写几何算法。
- AI 的 `AI_TRAFFIC_ASSIGNED_RUNWAY`（MSFS 直读，最可靠）；缺失时用"距该跑道 holding point 图距离最近"过滤。

输出：每跑道一条队列 `[{"callsign":…, "state":…, "eta_to_runway_s":…}]`，`request_takeoff_slot(callsign, runway)` 返回本机排位 N、前机、以及按尾流间隔计算的预计等待秒数。

**尾流类别与间隔必须用 ICAO Doc 4444 (Amd.9, 2020) 权威值，禁止自造秒数**：

| 类别 | 判据（最大认证起飞重量） |
|---|---|
| Light (L) | ≤ 7,000 kg |
| Medium (M) | > 7,000 kg 且 < 136,000 kg |
| Heavy (H) | ≥ 136,000 kg（Super 除外） |
| Super (J) | ICAO Doc 8643 指定（现役仅 A380-800） |

距离型间隔（进近/起飞，单位 NM）：

| 前机 \ 后机 | Heavy | Medium | Light |
|---|---|---|---|
| Super | 5.0 | 7.0 | 8.0 |
| Heavy | 4.0 | 5.0 | 6.0 |
| Medium | — | — | 5.0 |

时间型间隔（分钟）：着陆 HEAVY-behind-SUPER 2 / MEDIUM-behind-SUPER 3 / MEDIUM-behind-HEAVY 2 / LIGHT-behind-SUPER 4 / LIGHT-behind-HEAVY 或 MEDIUM 3。起飞的时间型间隔取决于是否全跑道起飞与起点位置，范围 **2–4 分钟**（Doc 4444 未给出单一固定表）。

实现口径（**起飞间隔取确定默认值，可配置**，避免"2–4 min"悬念落到实现期）：排队器用"最近一架已占用跑道/正在起飞的 AI 的类别 → 本机类别"查下表（秒），配置键 `traffic.wake_sep_seconds` 可整体覆盖：

| 前机 \ 后机 | Super | Heavy | Medium | Light |
|---|---|---|---|---|
| Super | 120 | 120 | 180 | 180 |
| Heavy | — | 90 | 120 | 180 |
| Medium | — | — | 60 | 120 |
| Light | — | — | — | 60 |

（"—"表示无强制尾流间隔，仅用跑道占用释放判定，取默认 60 s 冷却。）B757 按 FAA 规定视作 Heavy（即使 MTOW 落在 Medium）。`wake_category` 缺失时按机型粗分，再缺则取固定 120 s，并在 UI 标注"间隔为估算"。上表为工程默认值（源自 §7.D1 的 ICAO 时间型区间取保守端），非法规精确值——文档需注明。

### D2. 接入 ATC 决策链（G4）

- **Tier-0 插点**（`logic_manager.py`，`check_request` 返回 `allowed` 且 `action == "takeoff"` 之后，紧邻 :1027 的 runway_request 分支）：若 `tuned_role == "Tower"` 且队列非空，直接回"排在第 N 位，跟在前机之后"或把队列位置塞进 prompt 供 Tier-2/3 使用。注意插在**跑道/应答机 prereq 通过之后**，避免与 `missing_runway` 重定向冲突。
- **`atc_action` emit**：挂在 `_emit_instruction_cards`（:1216 定义 / :1222 调用）内，由 `InstructionExtractor` 卡片派生动作名（`cleared_takeoff` / `lineup_wait` / `contact` / `descend_to` …），`event_bus.emit('atc_action', action, params)`。这会同时激活 `plugin_manager.py:79` 已订阅但从未触发的钩子链（`plugin_api.py:275`）。同时在 Tier-0 redirect 分支 emit `contact` 动作。
- **prompt 注入**：`llm_client.py` 新增 `departure_queue` block，参照 CPDLC block（:529-534）的注入方式，仅 `TOWER_DEP` 阶段注入，内容为当前跑道队列与排位。
- 订阅 `traffic_state_change`（`traffic_manager.py:546` 已 emit）驱动队列重算。

### D3. UI/插件（可选）

- 队列经现有 SocketIO 通道推给前端雷达面板（`_emit_bulk_update`，`traffic_manager.py:367-388`）。
- 官方示例插件用 `on_atc_action` / `on_traffic_update`（`plugin_api.py:275/289`）+ `inject_panel`（:433）+ `set_status_bar_item`（:505）展示队列，验证钩子链且不侵入核心。

**阶段四改动文件**：新增 `core/departure_sequencer.py`；改 `core/logic_manager.py`、`core/llm_client.py`、`core/traffic_manager.py`、`core/atc_session.py`。

---

## 8. 工作块 E：配置、文档、i18n、快捷回复

- `config.example.json` 补齐三个**完全缺失**的段（E6）：

```json
"simulator":  { "provider": "auto", "xplane_host": "127.0.0.1", "xplane_root": "" },
"navdata":    { "frequency_source": "third_party", "ground_source": "simulator",
                "sqlite_path": "path/to/db", "procedure_source": "auto",
                "osm_overpass_url": "https://overpass-api.de/api/interpreter" },
"traffic":    { "enabled": true, "msfs_ai_enabled": true, "traffic_radius_nm": 40,
                "sequencer_enabled": false, "chatter_enabled": true,
                "wake_sep_seconds": null }
```

- `templates/settings.html` 增补 `navdata.sqlite_path`、`navdata.procedure_source`、`traffic.msfs_ai_enabled`、`traffic.traffic_radius_nm` 的读写（沿用 :801-802 与 :900-901 的两段式）。
- i18n 三处同步：`PHASE_LABEL_ZH/EN`（+ 新建 `PHASE_LABEL_JA`）、`sequence_for_ui()` 输出 `label_ja`、`dashboard.html:944` 的语言分支补 ja。
- `data/quick_reply_templates.json`：现有 categories 为 `acknowledgement/ground/tower/enroute/clearance/handoff/request`，新增 `dispatch` / `arrival_ground` / `gate`。新增模板可被 Tier-1 `QuickReplyEngine.auto_match`（`logic_manager.py:1043`）自动命中。
- README / docs 增加"各模拟器下游戏内数据可得性矩阵"：MSFS 编译 BGL 不可读（需 LNMP 或 dev-mode XML）；MSFS 侧 AI 交通经 SimConnect `RequestDataOnSimObjectType` 可读但依赖注入器形态（FSLTL live via Navigraph / 静态时刻表 / 无）；X-Plane TCAS 有位置与地速但无机型/尾流；X-Plane 程序需用户自备 `Custom Data/earth_424.dat` 且仅覆盖美国；LittleNavMap db 为全局 SID/STAR/进近最佳来源，路径 `%APPDATA%\ABarthel\little_navmap_db\`。

---

## 9. 实施顺序与每阶段验收

| 阶段 | 内容 | 独立验收标准 |
|---|---|---|
| 一 | 工作块 A + A4 bug 修复 + 测试同步 | IFR 下 mock 遥测驱动 `DISPATCH→ATIS→…→PARKED` 全程推进；**VFR 下初态为 ATIS、绝不停在 DISPATCH**；到达侧 `gate_assignment`/`taxi_to_gate` 权限与 prereq 条件生效；`python -m unittest discover -s tests` 全绿；`python -m py_compile app.py core/*.py` 通过 |
| 二 | 工作块 B | 样例 LNMP db 与 `earth_424.dat` 样本跑通 `procedure_service`，`source` 标记正确；无数据时回落 `llm` 标志；`/import_simbrief` 回归正常且返回 `sid`/`star`/`dep_rwy`/`arr_rwy` |
| 三 | 工作块 C | 真实 MSFS+FSLTL 上读到 AI 呼号/位置/机型/assigned runway；连接失败时降级 mock 且日志可追溯 |
| 四 | 工作块 D | 合成交通列表下队列顺序与"第 N 位"计算正确；`atc_action` 在 `_emit_instruction_cards` 后 emit 且插件收到 |
| 五 | 工作块 E | `config.example.json` 复制即可启动；设置页改配置即时生效；`PHASE_LABEL_JA` 全链路显示 |

每阶段一个分支，合入前跑 `tests/` 全量 + 冒烟。

---

## 10. 验证矩阵

**离线可跑（当前 Linux 环境）**：

1. 前置：`pip install -r requirements.txt`（本机缺 `networkx`，不装则 `tests/` 直接 ImportError——G6）。
2. `python -m unittest discover -s tests -v`。
3. `python -m py_compile app.py core/*.py`（注意 `app.py` 带 UTF-8 BOM，必须先 `encoding='utf-8-sig'` 读取；**不要**用 `python -c "import app"` 当冒烟，那会拉起 Flask 服务）。
4. 阶段机：mock 遥测序列驱动 `observe_telemetry`，断言完整推进与 prereq/权限分支。
5. 程序服务：样例 LNMP db（或自建最小 schema 库）+ `earth_424.dat` 样本断言解析与 `source` 回退。
6. 排队器：合成交通列表断言队列顺序、排位、尾流间隔（按 §7.D1 表逐组对拍，含 Super/Heavy/Medium/Light 组合）。
7. 路由回归：`/import_simbrief`、`/api/taxi_route`（以及到达侧新增接口）。

**必须真实模拟器（本环境不可验证）**：

1. C1 的 `RequestDataOnSimObjectType` 是否真的枚举到 FSLTL 注入对象（呼号/位置/机型/assigned runway/assigned parking）；重点验 radius clamp（40 NM 正常、超过 108 NM 是否触发 `SIMCONNECT_EXCEPTION_OUT_OF_BOUNDS`）。
2. D1 申请起飞时队列位置与画面中排队一致；分别在 FSLTL live、FSLTL 静态、无注入器三种现场各测一次。
3. X-Plane 侧 TCAS 读取未被回归破坏。

以上项在文档中如实标注"需实机验证"，禁止在离线环境谎报通过。

## 11. 外部资料（已核实可引用）

- [SimConnect_RequestDataOnSimObjectType — MSFS 官方文档](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Events_And_Data/SimConnect_RequestDataOnSimObjectType.htm)：radius 单位米、上限 200 km、`SIMCONNECT_SIMOBJECT_TYPE` 取值。
- [Missing SimVar in the Documentation — MSFS DevSupport](https://devsupport.flightsimulator.com/t/missing-simvar-in-the-documentation/4364)：`AI TRAFFIC *` 系列未文档化但可用。
- [Wake turbulence category — Wikipedia（汇总 ICAO Doc 4444 Amd.9）](https://en.wikipedia.org/wiki/Wake_turbulence_category)：尾流类别判据、距离型/时间型间隔表。
- [LittleNavMap `src/query/procedurequery.cpp`](https://github.com/albar965/littlenavmap/blob/master/src/query/procedurequery.cpp)：`approach` / `approach_leg` / `transition` / `transition_leg` 表结构、`type` 编码（`D`=SID、`A`=STAR）、机场关联键 `airport_ident`。
- [navdatareader releases](https://github.com/albar965/navdatareader/releases)：schema 随版本加列（`vertical_angle`、`arinc_type`）的证据。
- [FSLTL Impacted by FlightRadar24 API Shutdown — FSNews](https://fsnews.eu/fsltl-impacted-by-flightradar24-api-shutdown/) 与 [simdispatch 工具现状](https://simdispatch.de/2026/06/10/best-free-tools-msfs-2020-2024/)：FR24 API 2026-04-30 关闭、v1.9.0 转静态、2026-05-26 Navigraph 授权恢复 live。
- [CIFP-Updater README](https://github.com/TripleJumpStudios/CIFP-Updater)：X-Plane 侧 `Custom Data/earth_424.dat` 的放置方式（单文件、FAA CIFP 改名）。
- [Navigraph 论坛：SimBrief `sid_ident`/`star_ident` 截断行为](https://forum.navigraph.com/t/wishlist-for-the-simbrief-json-xml-file/19846)。

---

## 12. 风险与回滚

| 风险 | 缓解 |
|---|---|
| SimConnect 线程重入 dispatch | 独立连接实例（推荐）或全局锁；失败自动降级 mock |
| LNMP 库 schema 版本差异 | 一律 `select *` + 键名容错，不写死列清单；navdatareader 已多次加列（`vertical_angle`、`arinc_type`）；缺列即回落 LLM |
| X-Plane `earth_424.dat` 只覆盖美国 | 仅作为美国机场的程序源；非美国机场直接走 SimBrief/LLM，不视为失败 |
| FSLTL 形态多变（live via Navigraph / 静态时刻表 / 未运行） | 交通源三态检测 + 日志标注；空队列即放行；队列功能不依赖 live |
| SimBrief ident 截断导致匹配失败 | 匹配函数做 1 字符前缀容错 |
| DISPATCH 插入破坏既有联络顺序 | 阶段一同步更新 `tests/test_atc_session.py` 中依赖 PHASES 顺序的 4 组用例（SequenceTests :87 / TelemetryAdvanceTests :261 / ReleaseHandoffTests :278 / LogicManagerWiringTests :336） |
| **VFR/通航航班被强制走 DISPATCH 死锁（E12）** | `DISPATCH` 仅对 IFR 生效；VFR 初态 `ATIS`，`observe_telemetry` 加 VFR 跳过分支；新增 IFR/VFR 两组初态回归用例（见 §4.A1） |
| DISPATCH 与 CD 同频导致调频归属歧义（E13） | DISPATCH 排除出 `advance_to_role` 频率反查；PDC 走 ACARS/CPDLC 数据链，与语音调频解耦 |
| MSFS2020/2024 行为差异 | `requirements.txt` 锁 SimConnect 版本；启动探测 DLL 能力并记录版本 |
| 新数据源引入幻觉 | 每个源带 `source` 字段；`llm` 兜底仅在 `procedure_source` 允许时启用 |

回滚策略：阶段级 git 分支；`procedure_source=off`、`traffic.msfs_ai_enabled=false`、`traffic.sequencer_enabled=false` 三个开关各自独立关闭对应能力，回到 HEAD 行为。

---

## 13. 关键代码位置速查

```
core/atc_session.py          28 PHASES | 34 PHASE_ROLE | 47 标签字典(无JA) | 61 ROLE_LABEL_ZH
                             71 ROLE_FREQ_FALLBACKS | 82 PHASE_AUTHORITY | 96 ACTION_OWNER
                             112 ACTION_PREREQS | 119 _INTENT_PATTERNS | 165 STICKY_FIELDS
                             324 初态phase | 345 PROMPT_FIELDS | 498 observe_telemetry
                             718 check_request | 821 sequence_block | 1008 _fix_handoff_frequency
                             1054 enforce_message
core/logic_manager.py        466 _find_frequency_entry | 499 switch_frequency_context
                             530 _determine_controller 兜底 | 621 频段表 | 989 Tier-0
                             1090 phase 读取 bug | 1216 _emit_instruction_cards
                             1290 _update_issued_instructions
core/traffic_manager.py      15 TrafficState | 27 AircraftTrackingData | 248 _scan_traffic
                             291 _scan_xplane_tcas | 325 _process_ai_object | 367 _emit_bulk_update
                             412 update_aircraft | 546 traffic_state_change
core/taxi_router.py          36 stand_name 挂载 | 171 _find_runway_entry_nodes | 237 _mark_hotspots
core/xplane_provider.py      360 get_traffic_targets(无机型/尾流字段)
core/simconnect_provider.py  30 SimConnect 句柄
core/osm_ground_service.py   215 _attach_parking_to_taxi_network(stand入图样板)
core/simulator_ground_service.py  110 _discover_xplane_apt_files | 433-447 1300/1301 停机位
core/msfs_ground_service.py  1-7 BGL 限制 docstring
core/atc_handoff.py          24 ATCPhase 枚举 | 137 重复 socket emit
core/plugin_manager.py       79 on_atc_action 订阅(从未触发)
core/plugin_api.py           275/289/433/505 插件钩子
app.py                       122-183 SimBrief 解析/归一 | 1283 /import_simbrief
templates/settings.html      801-802 navdata 读取 | 900-901 写回
templates/dashboard.html     930 renderContactSequence | 944 语言分支 | 971 atc_phase_update
tests/test_atc_session.py    87/261/278/336 依赖 PHASES 顺序的四组用例
python-SimConnect 0.4.8      Attributes.py:88 RequestDataOnSimObjectType 绑定
                             Enum.py:140 AIRCRAFT=2 | SimConnect.py:9 自带dll
                             SimConnect.py:88 BYTYPE 分发 | 197 request_data 写死 USER
                             RequestList.py:1082 AI_TRAFFIC_* SimVar 表
外部权威资料                ICAO Doc 4444 Amd.9 尾流表 | MSFS SimConnect 官方文档
                             LNM src/query/procedurequery.cpp:2264-2345,2521-2578
                             earth_424.dat 放置方式(CIFP-Updater) | FSLTL/FR24 时间线
```
