# 各模拟器下游戏内数据可得性矩阵

本文件回答一个问题：**在用户当前的模拟器里，哪些"真实数据"OpenFrequency 拿得到，拿不到时走什么降级路径**。所有结论都对着代码与外部资料核实过；没核实过的项一律标注"需实机验证"。

## 总览

| 数据类别 | MSFS 2020/2024 | P3D / FSX | X-Plane 12 |
|---|---|---|---|
| 机场频率（塔台/地面/放行/进近…） | OurAirports CSV / LittleNavMap db；`navdata.frequency_source` 可切模拟器原生 | 同 MSFS（SimConnect） | 同 MSFS；X-Plane 侧还可读 apt.dat 频率行 |
| 地面滑行图（滑行道/节点/边） | **编译 BGL 不可读**（`core/msfs_ground_service.py` docstring 已述），依赖 LittleNavMap db 或 dev-mode XML；否则回落 OSM + LLM | 同 MSFS | apt.dat 可读（`Resources/default scenery/default apt dat/Earth nav data/apt.dat` 及 Custom Scenery） |
| AI 交通（呼号/位置/机型/分配跑道） | SimConnect `RequestDataOnSimObjectType(AIRCRAFT)` 可枚举；形态取决于注入器 | 同 MSFS | TCAS dataref 有位置/地速/高度，**无机型、无尾流、无分配跑道** |
| 程序（SID/STAR/进近） | LittleNavMap db（全局最佳）→ `earth_424.dat`（仅美国）→ SimBrief OFP → LLM 兜底 | 同 MSFS | 同 MSFS；X-Plane 额外可用 `Custom Data/earth_424.dat`（仅美国） |
| PDC（预放行） | ACARS(Hoppie)/CPDLC 数据链，不占用语音频率 | 同 MSFS | 同 MSFS |

## 关键细节

### 1. MSFS 地面数据：BGL 是硬限制
MSFS 把机场滑行网络编译进 BGL 封装，运行时没有公开 API 可读。可用替代：
- LittleNavMap 数据库（推荐，见下）；
- MSFS dev-mode 导出的 XML；
- OpenStreetMap / Overpass（`navdata.ground_source=osm`，全球可用但精度依赖 OSM 数据）。

### 2. MSFS AI 交通：SimConnect 底层枚举
`AircraftRequests` 通道把 `RequestDataOnSimObjectType` 的 type/radius 写死为 `USER`/0，永远只读用户机。OpenFrequency 因此在 `core/simconnect_traffic.py` 里自建 data definition + request id，自收 `SIMCONNECT_RECV_ID_SIMOBJECT_DATA_BYTYPE`，并用**独立 SimConnect 连接实例 + 独立 dispatch 线程**（SimConnect 实例非线程安全，允许多 client 连同一 sim）。
半径 clamp：MSFS 官方上限 200,000 m（≈108 NM），超出抛 `SIMCONNECT_EXCEPTION_OUT_OF_BOUNDS`；本实现配置上限 100 NM。
`AI TRAFFIC *` 系列 SimVar 未文档化但实测可用（DevSupport 官方确认），读取失败字段留空。

**交通源三态**（FSLTL 的 FR24 免费 API 已于 2026-04-30 关闭，injector 一度退化为纯静态时刻表注入 v1.9.0；2026-05-26 Navigraph 授权后恢复 live 注入）：
| 形态 | 特征 | OpenFrequency 行为 |
|---|---|---|
| live | AI 数量随时间增减，`AI TRAFFIC ETA` 非空且在变 | 正常枚举/排队 |
| static | AI 按时表移动，密度低 | 正常枚举；排队可能为空 |
| none | 注入器未运行，零 AI | 空队列即放行；若 `debug.mock_traffic_fallback` 为真则退 mock |

状态栏/日志会标注当前检测到的形态（每 5 s 推 `traffic_source_state`）。

### 3. X-Plane 程序数据
X-Plane 的 CIFP 是**单个全量文件** `<X-Plane>/Custom Data/earth_424.dat`（由 FAA CIFP 改名放入，见 CIFP-Updater），不是 per-ICAO 文件，且**只覆盖美国**。`procedure_service` 按 ICAO 前缀流式过滤；非美国机场自动落到 SimBrief/LLM，不算失败。

### 4. LittleNavMap 数据库（推荐开启）
SID/STAR/进近的全局最佳来源。典型路径（写进设置页 `navdata.sqlite_path`）：
```
%APPDATA%\ABarthel\little_navmap_db\little_navmap_navigraph.sqlite
%APPDATA%\ABarthel\little_nav_map_db\little_navmap_msfs.sqlite
```
SID/STAR/进近统一在 `approach` 表（`type`: `D`=SID、`A`=STAR，其余为进近），关联键是 `airport_ident`；schema 随版本加列，实现一律 `select *` + 按需取值，缺列回落下一级数据源。

### 5. SimBrief 标识截断
SimBrief 会把 SID/STAR 标识截断 1 个字符（`OBOKA4G → OBOK4G`，官方确认行为）。与本地库匹配时做 1 字符前缀容错。

## 最小可用配置

```json
{
  "simulator": { "provider": "auto", "xplane_host": "127.0.0.1", "xplane_root": "" },
  "navdata": {
    "frequency_source": "third_party",
    "ground_source": "simulator",
    "sqlite_path": "path/to/little_navmap_navigraph.sqlite",
    "procedure_source": "auto",
    "osm_overpass_url": "https://overpass-api.de/api/interpreter",
    "traffic_radius_nm": 40
  },
  "traffic": {
    "enabled": true,
    "msfs_ai_enabled": true,
    "traffic_radius_nm": 40,
    "sequencer_enabled": false,
    "chatter_enabled": true,
    "wake_sep_seconds": null
  }
}
```

三个回滚开关：`navdata.procedure_source=off`、`traffic.msfs_ai_enabled=false`、`traffic.sequencer_enabled=false` 各自关闭对应能力，回到升级前行为。

## 需实机验证项（离线环境不可验证）

1. 真实 MSFS + FSLTL 下 `RequestDataOnSimObjectType` 是否枚举到 AI（呼号/位置/机型/assigned runway/parking）；重点验 radius clamp（40 NM 正常、>100 NM 是否触发 `SIMCONNECT_EXCEPTION_OUT_OF_BOUNDS`）。
2. 申请起飞时队列位置与画面中排队一致；FSLTL live / 静态 / 无注入器三种现场各测一次。
3. X-Plane 侧 TCAS 读取未被回归破坏。

## 参考

- [SimConnect_RequestDataOnSimObjectType — MSFS 官方文档](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Events_And_Data/SimConnect_RequestDataOnSimObjectType.htm)
- [Missing SimVar in the Documentation — MSFS DevSupport](https://devsupport.flightsimulator.com/t/missing-simvar-in-the-documentation/4364)
- [LittleNavMap procedurequery.cpp](https://github.com/albar965/littlenavmap/blob/master/src/query/procedurequery.cpp)
- [CIFP-Updater（earth_424.dat 放置方式）](https://github.com/TripleJumpStudios/CIFP-Updater)
- [FSLTL impacted by FlightRadar24 API shutdown](https://fsnews.eu/fsltl-impacted-by-flightradar24-api-shutdown/)
- [Wake turbulence category（ICAO Doc 4444 Amd.9 汇总）](https://en.wikipedia.org/wiki/Wake_turbulence_category)
