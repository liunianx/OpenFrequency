"""
procedure_service.py — SID/STAR/进近程序数据源（B1）。

源优先级链（每类数据返回结构带 source 字段，任一源命中即返回）：
  1. LittleNavMap SQLite（navdata.sqlite_path）——全局 SID/STAR/进近最佳来源
  2. X-Plane earth_424.dat（FAA CIFP，只覆盖美国）
  3. SimBrief OFP（sid_ident/star_ident/plan_rwy，随 flight_plan 传入）
  4. LLM 兜底：返回 []，由调用方按 source='llm' 自行生成

关键事实（已对照 LittleNavMap 官方源码 procedurequery.cpp 核实）：
  · LNM 把 SID/STAR/进近统一存在 approach 一张表，用 type 列区分：
    'D'=SID、'A'=STAR、其余为进近类型；关联键是 airport_ident（ICAO 字符串）。
  · schema 随版本加列（navdatareader 2025-11 给 approach_leg 加
    vertical_angle 等），因此一律 select * + row.keys() 取值，缺列走下一级。
  · SimBrief 会把 SID/STAR 标识截断 1 个字符（OBOKA4G → OBOK4G，官方确认行为），
    本地库匹配时做 1 字符前缀容错。
"""
from __future__ import annotations

import os
import sqlite3
import threading

# LNM approach.type 编码（procedurequery.cpp）
_LNM_SID = "D"
_LNM_STAR = "A"

# earth_424.dat（ARINC 424-18，132 字符定长记录）字段位置（0-based 切片）
# 布局依据 ARINC 424 §4.1.9 与开源解析器 arinc424（jack-laverty）列定义。
_A424_AIRPORT = slice(6, 10)        # Airport Identifier（ICAO）
_A424_APP_TYPE = 12                 # D=SID / E=STAR / F=Approach
_A424_IDENT = slice(13, 19)         # SID/STAR/Approach Identifier
_A424_ROUTE_TYPE = 19               # Route Type
_A424_TRANSITION = slice(20, 25)    # Transition Identifier
_A424_SEQ = slice(26, 29)           # Sequence Number
_A424_FIX = slice(29, 34)           # Fix Identifier
_A424_FIX_SECTION = slice(36, 38)   # Section Code (2)（fix 所属段，如 EA/PC/PG）
_A424_CONT_NO = 38                  # Continuation Record No（'0'/'1'=primary）
_A424_PATH_TERM = slice(47, 49)     # Path and Termination（IF/TF/RF/CF…）
_A424_TURN = 43                     # Turn Direction

_APP_TYPE_MAP = {"D": "SID", "E": "STAR", "F": "APPROACH"}


def _normalize_ident(value) -> str:
    return str(value or "").strip().upper()


def _runway_variants(runway) -> set:
    """'02R'/'2R'/'RW02R' → {'02R','2R','RW02R'} 等价集合，用于宽松匹配。"""
    if not runway:
        return set()
    text = str(runway).strip().upper()
    text = text.removeprefix("RW").removeprefix("RUNWAY").strip()
    text = text.lstrip("0") or "0"
    return {text, f"RW{text}", text.lstrip("0").zfill(2)}


def _ident_prefix_match(simbrief_ident: str, local_ident: str) -> bool:
    """SimBrief 会截断 1 个字符（OBOKA4G → OBOK4G）：任一方是另一方前缀即算命中。"""
    a, b = _normalize_ident(simbrief_ident), _normalize_ident(local_ident)
    if not a or not b:
        return False
    return a.startswith(b) or b.startswith(a)


class ProcedureService:
    """SID/STAR/进近查询。线程安全（内部缓存带锁）。"""

    def __init__(self, config=None, ground_service=None):
        self.config = config or {}
        self.ground_service = ground_service
        self._cache = {}
        self._cache_lock = threading.Lock()
        self._simbrief_plan = None

    # ── 对外接口 ────────────────────────────────────────────────────────────

    def get_sids(self, icao, runway=None) -> list:
        return self._query(icao, runway, "SID")

    def get_stars(self, icao, runway=None) -> list:
        return self._query(icao, runway, "STAR")

    def get_approaches(self, icao, runway=None) -> list:
        return self._query(icao, runway, "APPROACH")

    def get_procedures(self, icao, runway=None) -> dict:
        """一次取三类，返回 {'SID': [...], 'STAR': [...], 'APPROACH': [...]}。
        每个元素带 source 字段标明数据来源。"""
        return {
            "SID": self.get_sids(icao, runway),
            "STAR": self.get_stars(icao, runway),
            "APPROACH": self.get_approaches(icao, runway),
        }

    # ── 源链调度 ────────────────────────────────────────────────────────────

    def _source_pref(self) -> str:
        pref = ((self.config.get("navdata", {}) or {}).get("procedure_source") or "auto").lower()
        return pref if pref in ("auto", "lnm", "cifp", "simbrief", "llm", "off") else "auto"

    def _query(self, icao, runway, kind) -> list:
        icao = _normalize_ident(icao)
        if not icao or icao == "N/A":
            return []
        pref = self._source_pref()
        if pref == "off":
            return []

        cache_key = (icao, _normalize_ident(runway), kind)
        with self._cache_lock:
            if cache_key in self._cache:
                return self._cache[cache_key]

        chain = ["lnm", "cifp", "simbrief"]
        if pref != "auto":
            chain = [pref] if pref != "llm" else []
        result = []
        for source in chain:
            try:
                if source == "lnm":
                    result = self._from_lnm(icao, runway, kind)
                elif source == "cifp":
                    result = self._from_cifp(icao, runway, kind)
                elif source == "simbrief":
                    result = self._from_simbrief(icao, runway, kind)
            except Exception as e:
                print(f"ProcedureService: {source} lookup failed for {icao}/{kind}: {e}")
                result = []
            if result:
                break

        with self._cache_lock:
            self._cache[cache_key] = result
        return result

    # ── 源 1：LittleNavMap SQLite ───────────────────────────────────────────

    def _lnm_path(self):
        path = (self.config.get("navdata", {}) or {}).get("sqlite_path") or ""
        if not path or "path/to/db" in path or not os.path.exists(path):
            return None
        return path

    def _from_lnm(self, icao, runway, kind) -> list:
        path = self._lnm_path()
        if not path:
            return []
        want_type = _LNM_SID if kind == "SID" else (_LNM_STAR if kind == "STAR" else None)
        conn = None
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if want_type:
                cursor.execute(
                    "SELECT * FROM approach WHERE airport_ident = ? AND type = ?",
                    (icao, want_type))
            else:
                # 进近：type 既不是 D 也不是 A 的全部行
                cursor.execute(
                    "SELECT * FROM approach WHERE airport_ident = ? AND type NOT IN (?, ?)",
                    (icao, _LNM_SID, _LNM_STAR))
            rows = cursor.fetchall()
        except Exception as e:
            print(f"ProcedureService: LNM query failed — {e}")
            return []
        finally:
            if conn:
                conn.close()

        out = []
        for row in rows:
            keys = row.keys()
            rwy = row["runway_name"] if "runway_name" in keys else ""
            if runway and rwy:
                # LNM runway_name 形如 "20R"；做宽松匹配
                if _normalize_ident(runway) not in _runway_variants(runway) \
                        and not _ident_prefix_match(runway, rwy):
                    continue
            legs = self._lnm_legs(path, row["approach_id"])
            transitions = self._lnm_transitions(path, row["approach_id"])
            out.append({
                "ident": _normalize_ident(row["arinc_name"] if "arinc_name" in keys else ""),
                "type": kind,
                "runway": _normalize_ident(rwy) or None,
                "transitions": transitions,
                "legs": legs,
                "source": "lnm",
            })
        return out

    def _lnm_legs(self, path, approach_id) -> list:
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM approach_leg WHERE approach_id = ? ORDER BY approach_leg_id",
                    (approach_id,))
                rows = cursor.fetchall()
            finally:
                conn.close()
        except Exception as e:
            print(f"ProcedureService: LNM legs query failed — {e}")
            return []
        legs = []
        for row in rows:
            keys = row.keys()
            if "fix_ident" in keys and row["fix_ident"]:
                leg = {"fix": _normalize_ident(row["fix_ident"])}
                if "is_missed" in keys:
                    leg["missed"] = bool(row["is_missed"])
                legs.append(leg)
        return legs

    def _lnm_transitions(self, path, approach_id) -> list:
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM transition WHERE approach_id = ?", (approach_id,))
                rows = cursor.fetchall()
            finally:
                conn.close()
        except Exception:
            return []
        out = []
        for row in rows:
            keys = row.keys()
            name = row["fix_ident"] if "fix_ident" in keys else None
            name = _normalize_ident(name)
            if name and name not in out:
                out.append(name)
        return out

    # ── 源 2：X-Plane earth_424.dat（ARINC 424-18，仅美国） ──────────────────

    def _discover_cifp_files(self) -> list:
        """探测 earth_424.dat；ground_service 有 discover_cifp_files 时优先复用。"""
        finder = getattr(self.ground_service, "discover_cifp_files", None)
        if callable(finder):
            try:
                return [p for p in (finder() or []) if os.path.exists(p)]
            except Exception as e:
                print(f"ProcedureService: ground_service CIFP discovery failed: {e}")
        sim_config = self.config.get("simulator", {}) or {}
        candidates = []
        explicit = sim_config.get("xplane_cifp_path")
        if explicit and os.path.exists(explicit):
            candidates.append(explicit)
        root = sim_config.get("xplane_root")
        if root and os.path.isdir(root):
            for rel in (("Custom Data", "earth_424.dat"),
                        ("Resources", "default data", "earth_424.dat")):
                candidate = os.path.join(root, *rel)
                if os.path.exists(candidate):
                    candidates.append(candidate)
        return candidates

    def _from_cifp(self, icao, runway, kind) -> list:
        files = self._discover_cifp_files()
        if not files:
            return []
        want_app = {"SID": "D", "STAR": "E", "APPROACH": "F"}[kind]
        rwy_variants = _runway_variants(runway)
        # ident -> {"transitions": {trans: [legs]}, "runways": set(), ...}
        procedures = {}
        for path in files:
            try:
                with open(path, "r", encoding="latin-1", errors="replace") as f:
                    for line in f:
                        if len(line) < 50:
                            continue
                        # 快速 ICAO 过滤，避免无关行的切片开销
                        if line[_A424_AIRPORT] != icao:
                            continue
                        if line[4] != "P" or line[_A424_APP_TYPE] != want_app:
                            continue
                        if line[_A424_CONT_NO] not in ("0", "1"):
                            continue  # 只取 primary 记录（含 E/P/W 延续的跳过）
                        ident = _normalize_ident(line[_A424_IDENT])
                        if not ident:
                            continue
                        trans = _normalize_ident(line[_A424_TRANSITION])
                        fix = _normalize_ident(line[_A424_FIX])
                        fix_section = _normalize_ident(line[_A424_FIX_SECTION])
                        path_term = _normalize_ident(line[_A424_PATH_TERM])
                        seq = _normalize_ident(line[_A424_SEQ]) or "0"
                        proc = procedures.setdefault(
                            ident, {"transitions": {}, "runways": set(), "types": set()})
                        proc["transitions"].setdefault(trans or "ALL", []).append(
                            {"seq": seq, "fix": fix, "section": fix_section,
                             "path_term": path_term, "turn": line[_A424_TURN]})
                        if fix_section == "PG":
                            proc["runways"].add(fix)
                        if trans:
                            proc["runways"].add(trans)
            except OSError as e:
                print(f"ProcedureService: CIFP read failed {path}: {e}")
                continue

        out = []
        for ident, proc in sorted(procedures.items()):
            if rwy_variants and proc["runways"]:
                if not any(v in rwy_variants for v in proc["runways"]):
                    continue
            legs = [leg for trans_legs in proc["transitions"].values() for leg in trans_legs]
            legs.sort(key=lambda item: (item["seq"], item["fix"]))
            out.append({
                "ident": ident,
                "type": kind,
                "runway": next((v for v in proc["runways"] if v), None),
                "transitions": sorted(t for t in proc["transitions"] if t != "ALL"),
                "legs": legs,
                "source": "cifp",
            })
        return out

    # ── 源 3：SimBrief OFP ──────────────────────────────────────────────────

    def _from_simbrief(self, icao, runway, kind) -> list:
        # SimBrief 数据由调用方随 flight_plan 提供；此处只做标识校准输出。
        fp = self._simbrief_plan or {}
        if not fp:
            return []
        if kind == "SID":
            ident, plan_rwy, endpoint = fp.get("sid"), fp.get("dep_rwy"), fp.get("origin")
        elif kind == "STAR":
            ident, plan_rwy, endpoint = fp.get("star"), fp.get("arr_rwy"), fp.get("destination")
        else:
            return []
        ident = _normalize_ident(ident)
        if not ident or ident == "N/A":
            return []
        if icao != _normalize_ident(endpoint):
            return []
        plan_rwy = _normalize_ident(plan_rwy)
        if runway and plan_rwy and plan_rwy not in _runway_variants(runway):
            return []
        return [{"ident": ident, "type": kind, "runway": plan_rwy or None,
                 "transitions": [], "legs": [], "source": "simbrief"}]

    def set_simbrief_plan(self, plan: dict | None):
        """注入 SimBrief 归一化后的飞行计划（origin/destination/sid/star/dep_rwy/arr_rwy）。"""
        self._simbrief_plan = plan or None
        with self._cache_lock:
            self._cache.clear()
