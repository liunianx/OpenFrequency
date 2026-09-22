# OpenFrequency 改进计划：签派到停机全流程 ATC + 游戏内数据 + FSLTL 联动

## Context（背景与目标）

OpenFrequency 是一个面向 MSFS / X-Plane 的开源 AI 空管系统（Flask + SocketIO + LLM）。用户提出三个目标：

1. **全流程 ATC 对话**：从签派/放行到落地停机，首尾完整。
2. **数据尽量取自游戏内**，取不到时回退 SimBrief / 公开网络源（滑行道、频率等）。
3. **与 FSLTL Live Traffic 联动**（MSFS 优先）：读取真实 AI 交通、离场排队，申请起飞时加入队列。

### 现状勘查结论（已读代码验证）

- **阶段机已存在但首尾是残桩**。`core/atc_session.py:28` 的 `PHASES` 已经排到 `ATIS→CLEARANCE→GROUND_DEP→TOWER_DEP→DEPARTURE→CENTER→APPROACH→TOWER_ARR→GROUND_ARR→PARKED`，有遥测驱动的自动推进（`observe_telemetry` `atc_session.py:498`）和跨管制员权威字段（`assign`/`STICKY_FIELDS`）。但：
  - **没有签派/PDC 前置阶段**；`CLEARANCE` 模板把 SID 伪造成 `route.split()[0]`（`atc_template_responder.py:38`，`llm_client.py:377-379`）。
  - **到达侧是空壳**：`GROUND_ARR` 权限只有 `{"taxi"}`，`PARKED` 角色为 `None`、权限为空（`atc_session.py:91-92`）；无廊桥分配、无进港滑行路由、无停机引导。推出仅一句硬编码 `"pushback approved, face west"`（`atc_template_responder.py:46`）。
- **数据源**：频率/跑道/机场位置来自 OurAirports CSV（`airport_frequency_service.py:13-15`）；地面布局来自模拟器磁盘文件或 OSM（`ground_data_service.py:29-45`）；METAR 来自 AviationWeather.gov；飞行计划来自 SimBrief（`app.py:1283`）。**无 SID/STAR/进近程序数据库**；MSFS 编译 BGL 场景不可读，回退 OSM（`msfs_ground_service.py` docstring）。SimConnect **只暴露本机遥测**。
- **交通/FSLTL**：X-Plane 侧通过 TCAS datarefs 读 LiveTraffic 是**真实可用**的（`xplane_provider.py:360-438`，`traffic_manager.py:291-323`）；**MSFS/SimConnect 侧完全是 mock**（`_scan_traffic` `traffic_manager.py:248-289` 里 `hasattr(sm,'get_ai_aircraft_list')` 永远为假 → `_generate_enhanced_mock_traffic`）。`python-SimConnect` 库**不能枚举 AI SimObject**，需用底层 `RequestDataOnSimObjectType(AIRCRAFT)`（ctypes/SDK）。**无排队概念**；插件钩子 `on_atc_action` 已订阅但**从未被 emit**（`plugin_manager.py:79`，无任何 `event_bus.emit('atc_action', …)`）。

### 用户决策（已确认）
- 目标模拟器：**MSFS 优先**（新增 SimConnect AI 枚举读 FSLTL）。
- 程序数据：**两者结合**——有本地导航库（LittleNavMap / X-Plane CIFP）时用真实 SID/STAR，无则回退 LLM。
- 流程范围：**补全两端**（签派/PDC + 到达侧廊桥/进港滑行/停机）。
- 交付形态：**详细规划文档**（本文件），实现在批准后进行。

---

## 总体架构原则

- **复用现有阶段机与权威字段**，不另起炉灶。所有新阶段/新指令都注册进 `atc_session.py` 的 `PHASES`/`PHASE_ROLE`/`PHASE_AUTHORITY`/`ACTION_OWNER`/`ACTION_PREREQS`，让 Tier-0 合法性检查、prompt 的 `sequence_block/authority_block/state_block`、`enforce_message` 自动生效。
- **数据获取统一走"源优先级链"**：游戏内(sim 文件/SimConnect) → 本地导航库 → SimBrief → 公开网络(OurAirports/OSM) → LLM 生成兜底。每类数据在返回结构里带 `source` 字段（现有 ground_summary 已有 `source`，沿用此约定）。
- **FSLTL/交通**：MSFS 走新的 SimConnect AI 枚举提供者；排队逻辑做成模拟器无关的 `DepartureSequencer`，X-Plane 复用现有 TCAS 通道。通过已存在但闲置的 `atc_action` 事件链把队列接进 ATC 决策。

---

## 工作分解（5 个工作块，可分阶段实施）

### 工作块 A：全流程阶段补全（签派 + 到达侧）

**A1. 新增签派/PDC 前置阶段**
- `atc_session.py:28` `PHASES` 在 `ATIS` 前（或 `ATIS` 与 `CLEARANCE` 间）插入 `DISPATCH`；补 `PHASE_ROLE["DISPATCH"]="Dispatch"`、中英标签、`PHASE_AUTHORITY["DISPATCH"]={"pdc","flightplan_confirm"}`。
- 新增意图 `pdc`（请求预放行）到 `_INTENT_PATTERNS`（`atc_session.py:119`）与 `ACTION_OWNER`。
- `atc_template_responder.py` 增加 `request_pdc` 模板：回读 SimBrief 航路/巡航/离场跑道（复用 `shared_context['flight_plan']`）。
- 遥测推进：`observe_telemetry` 中 `DISPATCH→ATIS/CLEARANCE` 由"已确认飞行计划"标志驱动（新增 `state["fpl_confirmed"]`）。

**A2. 推出与开车细化（仍属 GROUND_DEP，但补状态跟踪）**
- 在 session state 增加 `pushback_done`、`engines_started` 布尔位；`ACTION_PREREQS["taxi"]=("pushback",)`（滑行前须已推出，除非停机位可直接滑出）。
- 推出方向不再硬编码：依据 `ground_summary` 的停机位朝向 / 邻接滑行道推断（`taxi_router.py` 已有节点几何，新增 `suggest_pushback_direction(stand)` 辅助）。

**A3. 到达侧补全（重点）**
- `PHASE_AUTHORITY["GROUND_ARR"]` 扩为 `{"taxi","gate_assignment"}`；新增意图 `gate_request` / `taxi_to_gate`。
- **进港滑行路由**：`taxi_router.py` 目前只算"到跑道入口"（`_find_runway_entry_nodes` `taxi_router.py:171`）。新增 `suggest_taxi_in_route(airport, aircraft_position, target_stand)`——起点为脱离跑道节点，终点为分配的 stand 节点（stand 节点数据已存在于 layout：`1300/1301` / OSM parking）。
- **廊桥分配**：新增 `core/gate_assigner.py`（或并入 `ground_data_service`）：从 layout 的 stand 列表按航司/机型/占用情况选一个到达 stand。占用信息可选用 FSLTL 交通位置去重（见工作块 D）。
- `llm_client.py:491-517` 的 `ground_help` 目前只在 `"Ground" in role` 且填充**离场**路由时生效；扩展为到达侧也填充 `suggested_taxi_in_route` 与 `assigned_gate`。
- `PARKED` 阶段：给一个到达服务播报（欢迎/关车/地面服务），`enforce_message` 在 `atc_session.py:1035` 对 `PARKED` 跳过 handoff 注入的逻辑保留，但允许播报文本。

**A4. 修复已发现的既有 bug（顺带）**
- `logic_manager.py:1090` Tier-2 fast prompt 读 `ctx_snapshot['flight']['phase']`（该键不存在，phase 恒为 unknown）——改读 `atc_state.session.phase`。
- `_determine_controller`（`logic_manager.py:621-647`）Clearance Delivery 频段过窄（`118.95<f<119.0`）导致放行常被误判为 Tower——改为用机场频率库里 CD 的真实频率匹配。
- 评估 `atc_handoff.ATCHandoffManager` 与 `logic_manager` 双重驱动 session 的冗余，保留一个（logic_manager 为主）。

**关键文件**：`core/atc_session.py`、`core/atc_template_responder.py`、`core/taxi_router.py`、`core/llm_client.py`、`core/logic_manager.py`、新增 `core/gate_assigner.py`。

---

### 工作块 B：程序数据源（SID/STAR/进近，真实优先 + LLM 兜底）

**B1. 新增 `core/procedure_service.py`**，对外提供 `get_sids(icao,runway)` / `get_stars(icao,runway)` / `get_approaches(icao,runway)`，内部按源链：
1. **本地导航库**：
   - LittleNavMap SQLite（项目已有 sqlite 回退路径的先例：`airport_frequency_service.py:400-439`、`nav_manager.py`）。查询 `approach`/`transition`/`approach_leg` 表得到真实程序。
   - X-Plane CIFP：`Custom Data/CIFP/<ICAO>.dat`（ARINC-424 子集），复用 `simulator_ground_service.py` 已有的 X-Plane 路径发现逻辑（`_discover_xplane_apt_files` 同目录树）。
2. **SimBrief**：`app.py:1283` 的 OFP 已含 `navlog`；扩展解析出 OFP 里的 SID/STAR 名与航路点（OFP 有 `origin.plan_rwy`/`sid`/`star` 字段可取）。
3. **LLM 兜底**：无上述数据时，维持现状由 LLM 依据航路生成（保持行为不变，但显式标注 `source="llm"`）。
- 结果注入 session 的权威 sticky 字段（`sid`/`star`/`approach` 已在 `STICKY_FIELDS`），并在 prompt 里以 `source` 提示 LLM"有真实程序则逐字使用，否则可生成"。

**B2. 频率/程序的"游戏内优先"开关**
- 已有 `navdata.frequency_source`（`third_party`/`simulator`）与 `navdata.ground_source`。新增 `navdata.procedure_source` 配置项，默认 `auto`（走源链）。
- MSFS 编译 BGL 不可读是硬限制——文档中明确写明：MSFS 用户要拿真实程序/滑行道，需装 LittleNavMap（读其数据库）或提供 dev-mode XML；否则回退 OSM+LLM。

**关键文件**：新增 `core/procedure_service.py`；改 `core/airport_frequency_service.py`（复用其 sqlite 打开逻辑）、`core/simulator_ground_service.py`（CIFP 路径）、`app.py`（SimBrief SID/STAR 解析）、`config.example.json`（新增配置项）。

---

### 工作块 C：MSFS SimConnect AI 交通读取（FSLTL 的数据基础）

这是 FSLTL-on-MSFS 的核心阻塞项。`python-SimConnect` 无法枚举 AI 对象，需底层调用。

**C1. 新增 `core/simconnect_traffic.py`**（或 `SimConnectProvider.get_traffic_targets()`）：
- 通过 ctypes 直接调用底层 `SimConnect_RequestDataOnSimObjectType`，`SIMCONNECT_SIMOBJECT_TYPE_AIRCRAFT`，半径取用户可配（如 40 NM）。
- 每个 AI 对象读取 SimVars：`PLANE LATITUDE/LONGITUDE`、`PLANE ALTITUDE`、`GROUND VELOCITY`、`VERTICAL SPEED`、`PLANE HEADING DEGREES TRUE`、`SIM ON GROUND`、`ATC ID`（呼号）、`ATC MODEL`/`CATEGORY`（机型/尾流，供排队用）。
- 复用 `SimConnectProvider.sc`（`simconnect_provider.py:30` 已持有 `SimConnect()` 句柄，可取其底层 dll/handle）。
- FSLTL 注入的就是标准 SimConnect AI 对象，因此该机制天然覆盖 FSLTL。

**C2. 打通 `traffic_manager.py` 的 MSFS 路径**：
- `_scan_traffic`（`traffic_manager.py:248`）改为调用 C1 的枚举方法，用返回结果调 `update_aircraft`（`traffic_manager.py:412`），删除/降级 `_generate_enhanced_mock_traffic` 为"无数据时才用"。`_process_ai_object`（现死代码 `traffic_manager.py:325`）复活。
- **交通字段扩展**：`AircraftTrackingData`（`traffic_manager.py:27-52`）与 `update_aircraft` 增加 `aircraft_type`、`wake_category`、`squawk`（供排队尾流间隔与展示）。X-Plane 侧无这些字段则留空。

**关键文件**：新增 `core/simconnect_traffic.py`；改 `core/simconnect_provider.py`、`core/traffic_manager.py`。
**风险**：ctypes 绑定依赖具体 SimConnect DLL 版本；需在真实 MSFS + FSLTL 环境测试（当前 Linux 环境无法验证，见"验证"节）。

---

### 工作块 D：离场排队（DepartureSequencer）与"申请起飞加入队列"

**D1. 新增 `core/departure_sequencer.py`（模拟器无关）**：
- 输入：`traffic_manager` 的交通状态（含新的 state 枚举 `TAXIING/TAKEOFF_ROLL` 等，`traffic_manager.py:15-25` 已有）+ 用户机 phase/位置。
- 维护每条跑道的**排队序列**：识别正在向 holding point 滑行、已 line-up、正在起飞滚转的 AI，按接近顺序/时间排序。
- 当用户在 `TOWER_DEP` 申请起飞（intent `takeoff`，`atc_session.py:120`）时，计算用户在队列中的位置，返回"你是第 N 位，跟在 XXX 之后"，并按尾流间隔给出预计放行。
- 尾流/间隔用 C1 拿到的 `wake_category`；拿不到时用机型粗分或固定间隔。

**D2. 接入 ATC 决策链（复用闲置的 `atc_action` 事件）**：
- 现状：`plugin_manager` 订阅了 `atc_action` 但**无人 emit**（agent 已确认）。在 `logic_manager.on_llm_response` / `enforce_message` 产生结构化指令处（`atc_session.py:1054`、`logic_manager.py:1168`）**emit `atc_action`**，同时让 `DepartureSequencer` 订阅 `traffic_state_change`（`traffic_manager.py:546` 已 emit）。
- 起飞请求处理：`logic_manager.process_llm_request` 的 Tier-0（`logic_manager.py:997`）在 `takeoff` intent 时先查 `DepartureSequencer`，若前方有队列，则 Tier-0 直接回"排在第 N，稍等"或把队列位置塞进 LLM prompt，让塔台的放行/等待更真实。
- prompt 注入：在 `llm_client.py` 新增 `departure_queue` block（类似现有 CPDLC/ground block 的注入方式 `llm_client.py:529-534`），把当前跑道队列告诉 LLM。

**D3. UI/插件呈现（可选）**：
- 队列通过现有 SocketIO 推给前端雷达面板（`traffic_manager._emit_bulk_update` `traffic_manager.py:367` 同款通道）。
- 也可做成官方示例插件，用 `on_atc_action`/`on_traffic_update` + `inject_panel`/`set_status_bar_item`（`plugin_api.py:275,289,433,505`）展示队列——这样验证了插件钩子链且不侵入核心。

**关键文件**：新增 `core/departure_sequencer.py`；改 `core/logic_manager.py`、`core/llm_client.py`、`core/traffic_manager.py`、`core/atc_session.py`（emit atc_action）。

---

### 工作块 E：配置、文档、i18n 收尾
- `config.example.json`：新增 `navdata.procedure_source`、`traffic.msfs_ai_enabled`、`traffic.sequencer_enabled`、排队半径等。
- 文档：更新 `README` / `docs/`，明确各模拟器下"游戏内数据可得性"矩阵（尤其 MSFS 编译 BGL 限制、需要 LittleNavMap/FSLTL 的场景）。
- i18n：新增阶段/指令的中英日文案（`data/locales/*.json`、`atc_session.py` 的 `PHASE_LABEL_*`）。
- 快捷回复：`data/quick_reply_templates.json` 补 `dispatch`/`arrival_ground`/`gate` 类别（现无进港滑行/停机模板）。

---

## 建议实施顺序（分阶段）

1. **阶段一（工作块 A + A4 bug 修复）**：纯软件、可在无模拟器环境用 mock 遥测测试，收益最直接（首尾流程立刻完整）。
2. **阶段二（工作块 B）**：程序数据源，依赖用户本地导航库，可用样例 SQLite/CIFP 测试解析。
3. **阶段三（工作块 C）**：MSFS SimConnect AI 读取，**必须在真实 MSFS+FSLTL 环境验证**。
4. **阶段四（工作块 D）**：排队逻辑，建立在 C（或 X-Plane TCAS）之上。
5. **阶段五（工作块 E）**：配置/文档/i18n。

---

## 验证（Verification）

- **单元/离线**（本环境可跑）：
  - 阶段机：构造 mock 遥测序列驱动 `observe_telemetry`，断言 `DISPATCH→…→PARKED` 全程推进；断言到达侧 `gate_assignment`/`taxi_to_gate` 权限与 prereq 生效。测试放 `tests/`（项目已有 `tests/` 目录）。
  - 程序服务：用一个小型样例 LittleNavMap SQLite 和一段 CIFP 文本，断言 `procedure_service` 解析出 SID/STAR 且 `source` 正确；无数据时回退标志正确。
  - 排队器：喂入合成交通列表，断言队列顺序与用户"第 N 位"计算正确。
  - `python app.py` 启动无异常，`/import_simbrief`、`/api/taxi_route` 等路由回归正常。
- **需真实模拟器**（本环境无法验证，需用户在 Windows + MSFS + FSLTL 上测）：
  - 工作块 C 的 SimConnect AI 枚举是否真的读到 FSLTL 注入的 AI（呼号/位置/机型）。
  - 工作块 D 申请起飞时队列位置是否与画面中排队一致。
  - 计划中会把这些标注为"需实机验证"，不在离线环境谎报结果。
- 每次改动后运行 `tests/` 下测试与 `python -c "import app"` 冒烟，清理临时文件。

## 未决/风险
- MSFS 编译 BGL 场景的滑行道/程序**无法从游戏内直接读取**（技术硬限制）——真实数据需 LittleNavMap 或 dev-mode，否则回退 OSM+LLM。文档需讲清。
- ctypes 调 SimConnect DLL 与具体 SDK 版本耦合，跨 MSFS2020/2024 需回归。
- FSLTL 无独立 API，完全依赖其注入的 SimConnect AI 对象；若用户未运行 FSLTL 则退回 mock/无交通。
