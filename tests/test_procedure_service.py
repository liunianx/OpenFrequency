"""阶段二（工作块 B）回归测试：procedure_service 源链。

覆盖：
  - LittleNavMap SQLite（自建最小 schema）
  - earth_424.dat（ARINC 424-18 合成样本，按官方列布局构造）
  - SimBrief 源 + 1 字符截断容错
  - 源选择/关闭与空数据回落

运行：python -m unittest discover -s tests -v
"""
import os
import sqlite3
import tempfile
import unittest

from core.procedure_service import ProcedureService


# ── earth_424.dat（ARINC 424-18）合成记录 ────────────────────────────────────
# 列位置依据 ARINC 424 §4.1.9（PD/PE/PF 记录）：
#   r[0]=记录类型, r[1:4]=Customer, r[4]='P', r[6:10]=ICAO, r[12]=应用类型
#   (D=SID/E=STAR/F=Approach), r[13:19]=程序标识, r[19]=Route Type,
#   r[20:25]=过渡标识, r[26:29]=序号, r[29:34]=Fix, r[36:38]=Fix 所属段,
#   r[38]=Continuation No, r[43]=Turn, r[47:49]=Path/Terminator
def _a424_line(app, icao, ident, trans, seq, fix, fix_section, path_term):
    chars = [" "] * 132

    def put(pos, text):
        for i, ch in enumerate(text):
            chars[pos + i] = ch

    put(0, "S")
    put(1, "USA")
    put(4, "P")
    put(6, icao)
    put(12, app)
    put(13, ident.ljust(6))
    put(19, "5")                      # Route Type
    put(20, trans.ljust(5))
    put(26, seq.zfill(3))
    put(29, fix.ljust(5))
    put(36, fix_section.ljust(2))
    put(38, "0")                      # primary record
    put(43, " ")
    put(47, path_term.ljust(2))
    put(123, "00001")
    put(128, "2601")
    return "".join(chars) + "\n"


EARTH_424_SAMPLE = "".join([
    # KJFK SID LENDY4B：跑道过渡 RW13L + 公共航段
    _a424_line("D", "KJFK", "LENDY6", "RW13L", "010", "RW13L", "PG", "IF"),
    _a424_line("D", "KJFK", "LENDY6", "RW13L", "020", "LENDY", "PC", "IF"),
    _a424_line("D", "KJFK", "LENDY6", "", "010", "LENDY", "PC", "IF"),
    _a424_line("D", "KJFK", "LENDY6", "", "020", "CAMRN", "EA", "TF"),
    # KJFK SID DEEZZ5A：只服务 31L（跑道过滤用）
    _a424_line("D", "KJFK", "DEEZZ5", "RW31L", "010", "RW31L", "PG", "IF"),
    _a424_line("D", "KJFK", "DEEZZ5", "RW31L", "020", "DEEZZ", "PC", "IF"),
    # KJFK STAR SEEYR6
    _a424_line("E", "KJFK", "SEEYR6", "RW22L", "010", "SEEYR", "EA", "IF"),
    _a424_line("E", "KJFK", "SEEYR6", "", "010", "ROBER", "EA", "IF"),
    # KJFK 进近 I13L（ILS）与 RNP31L
    _a424_line("F", "KJFK", "I13L", "RW13L", "010", "RW13L", "PG", "TF"),
    _a424_line("F", "KJFK", "I13L", "RW13L", "020", "LENDY", "PC", "TF"),
    _a424_line("F", "KJFK", "RNP31L", "RW31L", "010", "RW31L", "PG", "TF"),
    # 别的机场（必须被 ICAO 过滤排除）
    _a424_line("D", "KLAX", "HOBTT1", "RW25R", "010", "RW25R", "PG", "IF"),
])


def _build_lnm_db(path):
    """按 LittleNavMap 真实 schema 建最小库（approach 表 type: D=SID, A=STAR）。"""
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE approach (
        approach_id INTEGER PRIMARY KEY, type TEXT, arinc_name TEXT,
        airport_ident TEXT, suffix TEXT, fix_ident TEXT, runway_name TEXT,
        runway_end_id INTEGER)""")
    cur.execute("""CREATE TABLE approach_leg (
        approach_leg_id INTEGER PRIMARY KEY, approach_id INTEGER,
        fix_ident TEXT, is_missed INTEGER)""")
    cur.execute("""CREATE TABLE transition (
        transition_id INTEGER PRIMARY KEY, approach_id INTEGER, fix_ident TEXT)""")
    # ZGGG：SID VIBOS2A（02L 过渡）、STAR NYB2A、ILS 20R 进近
    cur.execute("INSERT INTO approach VALUES (1,'D','VIBOS2A','ZGGG','','VIBOS','02L',1)")
    cur.execute("INSERT INTO approach VALUES (2,'A','NYB2A','ZGGG','','NYB','20R',1)")
    cur.execute("INSERT INTO approach VALUES (3,'I','I20R','ZGGG','','IF20','20R',1)")
    cur.execute("INSERT INTO approach_leg VALUES (1,1,'VIBOS',0)")
    cur.execute("INSERT INTO approach_leg VALUES (2,1,'AGPON',0)")
    cur.execute("INSERT INTO approach_leg VALUES (3,1,'RW02L',0)")
    cur.execute("INSERT INTO transition VALUES (1,1,'RW02L')")
    cur.execute("INSERT INTO approach_leg VALUES (4,2,'NYB',0)")
    cur.execute("INSERT INTO transition VALUES (2,2,'ALL')")
    cur.execute("INSERT INTO approach_leg VALUES (5,3,'IF20',0)")
    cur.execute("INSERT INTO transition VALUES (3,3,'RW20R')")
    conn.commit()
    conn.close()


class CifpSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cifp_path = os.path.join(self.tmp.name, "earth_424.dat")
        with open(self.cifp_path, "w", encoding="ascii") as f:
            f.write(EARTH_424_SAMPLE)
        cfg = {"navdata": {"procedure_source": "cifp"},
               "simulator": {"xplane_cifp_path": self.cifp_path}}
        self.svc = ProcedureService(cfg)

    def test_sid_listed_and_runway_filtered(self):
        sids = self.svc.get_sids("KJFK")
        idents = [s["ident"] for s in sids]
        self.assertEqual(idents, ["DEEZZ5", "LENDY6"])
        self.assertTrue(all(s["source"] == "cifp" for s in sids))
        self.assertTrue(all(s["type"] == "SID" for s in sids))
        rwy13 = self.svc.get_sids("KJFK", "13L")
        self.assertEqual([s["ident"] for s in rwy13], ["LENDY6"])
        rwy31 = self.svc.get_sids("KJFK", "31L")
        self.assertEqual([s["ident"] for s in rwy31], ["DEEZZ5"])

    def test_sid_legs_and_transitions(self):
        sid = self.svc.get_sids("KJFK", "13L")[0]
        self.assertIn("RW13L", sid["transitions"])
        fixes = [leg["fix"] for leg in sid["legs"]]
        self.assertIn("RW13L", fixes)
        self.assertIn("LENDY", fixes)
        self.assertEqual(sid["runway"], "RW13L")

    def test_star_and_approach(self):
        star = self.svc.get_stars("KJFK", "22L")[0]
        self.assertEqual(star["ident"], "SEEYR6")
        self.assertEqual(star["type"], "STAR")
        apps = self.svc.get_approaches("KJFK", "13L")
        self.assertEqual([a["ident"] for a in apps], ["I13L"])
        apps31 = self.svc.get_approaches("KJFK", "31L")
        self.assertEqual([a["ident"] for a in apps31], ["RNP31L"])

    def test_icao_prefix_filters_other_airports(self):
        # KLAX 的记录只能通过 KLAX 查询命中，绝不能混进 KJFK 的结果
        kjfk = [s["ident"] for s in self.svc.get_sids("KJFK")]
        self.assertNotIn("HOBTT1", kjfk)
        klax = [s["ident"] for s in self.svc.get_sids("KLAX")]
        self.assertEqual(klax, ["HOBTT1"])

    def test_unknown_airport_returns_empty(self):
        self.assertEqual(self.svc.get_sids("ZZZZ"), [])


class LnmSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "little_navmap_navigraph.sqlite")
        _build_lnm_db(self.db_path)
        cfg = {"navdata": {"procedure_source": "lnm", "sqlite_path": self.db_path}}
        self.svc = ProcedureService(cfg)

    def test_sid_from_lnm(self):
        sids = self.svc.get_sids("ZGGG")
        self.assertEqual([s["ident"] for s in sids], ["VIBOS2A"])
        self.assertEqual(sids[0]["source"], "lnm")
        self.assertEqual(sids[0]["runway"], "02L")
        fixes = [leg["fix"] for leg in sids[0]["legs"]]
        self.assertIn("AGPON", fixes)

    def test_star_from_lnm(self):
        stars = self.svc.get_stars("ZGGG")
        self.assertEqual([s["ident"] for s in stars], ["NYB2A"])

    def test_approach_excludes_sid_star_types(self):
        apps = self.svc.get_approaches("ZGGG")
        self.assertEqual([a["ident"] for a in apps], ["I20R"])

    def test_missing_db_returns_empty(self):
        svc = ProcedureService({"navdata": {"procedure_source": "lnm",
                                            "sqlite_path": "/nonexistent/x.sqlite"}})
        self.assertEqual(svc.get_sids("ZGGG"), [])

    def test_placeholder_path_returns_empty(self):
        svc = ProcedureService({"navdata": {"procedure_source": "lnm",
                                            "sqlite_path": "path/to/db"}})
        self.assertEqual(svc.get_sids("ZGGG"), [])


class SimbriefAndFallbackTests(unittest.TestCase):
    def test_simbrief_ident_with_one_char_truncation(self):
        svc = ProcedureService({"navdata": {"procedure_source": "simbrief"}})
        svc.set_simbrief_plan({"origin": "KJFK", "destination": "KLAX",
                               "sid": "LEND4B", "star": "SEEY5",
                               "dep_rwy": "13L", "arr_rwy": "24R"})
        sids = svc.get_sids("KJFK", "13L")
        self.assertEqual([s["ident"] for s in sids], ["LEND4B"])
        self.assertEqual(sids[0]["source"], "simbrief")
        stars = svc.get_stars("KLAX", "24R")
        self.assertEqual([s["ident"] for s in stars], ["SEEY5"])
        # 起点不匹配时不应返回
        self.assertEqual(svc.get_sids("KLAX"), [])
        # approaches 永远不来自 SimBrief
        self.assertEqual(svc.get_approaches("KLAX", "24R"), [])

    def test_source_off_returns_empty(self):
        svc = ProcedureService({"navdata": {"procedure_source": "off"}})
        svc.set_simbrief_plan({"origin": "KJFK", "sid": "LEND4B"})
        self.assertEqual(svc.get_sids("KJFK"), [])

    def test_llm_fallback_returns_empty_and_caller_marks_source(self):
        # procedure_source=llm（或 auto 且全无数据）→ 空列表，由调用方生成
        svc = ProcedureService({"navdata": {"procedure_source": "llm"}})
        self.assertEqual(svc.get_sids("KJFK"), [])
        svc2 = ProcedureService({"navdata": {"procedure_source": "auto"}})
        self.assertEqual(svc2.get_sids("KJFK"), [])

    def test_auto_chain_falls_through_lnm_to_cifp(self):
        with tempfile.TemporaryDirectory() as tmp:
            cifp_path = os.path.join(tmp, "earth_424.dat")
            with open(cifp_path, "w", encoding="ascii") as f:
                f.write(EARTH_424_SAMPLE)
            cfg = {"navdata": {"procedure_source": "auto", "sqlite_path": "/nonexistent.db"},
                   "simulator": {"xplane_cifp_path": cifp_path}}
            svc = ProcedureService(cfg)
            sids = svc.get_sids("KJFK", "13L")
            self.assertEqual([s["ident"] for s in sids], ["LENDY6"])
            self.assertEqual(sids[0]["source"], "cifp")

    def test_result_cached_per_airport_runway(self):
        svc = ProcedureService({"navdata": {"procedure_source": "simbrief"}})
        svc.set_simbrief_plan({"origin": "KJFK", "destination": "KLAX",
                               "sid": "LEND4B", "dep_rwy": "13L"})
        first = svc.get_sids("KJFK", "13L")
        svc.set_simbrief_plan({"origin": "KJFK", "sid": "OTHER7"})
        # 缓存键含机场+跑道，重新注入计划后应清缓存
        second = svc.get_sids("KJFK", "13L")
        self.assertEqual([s["ident"] for s in second], ["OTHER7"])
        self.assertIsNot(first, second)


class PromptBlockTests(unittest.TestCase):
    """B1：procedures prompt 块（真实程序逐字 / llm 兜底显式标注）。"""

    @staticmethod
    def _block(context_copy):
        from core.llm_client import LLMClient
        client = LLMClient.__new__(LLMClient)      # 绕过 __init__（无 genai 依赖）
        client.procedure_service = None
        return client._build_procedures_block(context_copy)

    def test_real_procedures_listed_with_source(self):
        block = self._block({"navigation": {"procedures": {
            "SID": [{"ident": "LENDY6", "type": "SID", "runway": "13L",
                     "transitions": ["RW13L"], "source": "cifp"}],
            "STAR": [], "APPROACH": [], "source": "cifp"}}})
        self.assertIn("LENDY6", block)
        self.assertIn("source: cifp", block)
        self.assertIn("VERBATIM", block)

    def test_llm_fallback_explicitly_marked(self):
        block = self._block({"navigation": {"procedures": {
            "SID": [], "STAR": [], "APPROACH": [], "source": "llm"}}})
        self.assertIn("source: llm", block)
        self.assertIn("MAY generate plausible procedure", block)

    def test_off_source_emits_nothing(self):
        block = self._block({"navigation": {"procedures": {
            "SID": [], "STAR": [], "APPROACH": [], "source": "off"}}})
        self.assertEqual(block, "")


class TruncationAssignTests(unittest.TestCase):
    """E4：SimBrief ident 截断 1 字符时，逻辑管理器仍能匹配本地库并写入权威字段。"""

    def test_truncated_simbrief_sid_matches_library(self):
        from core.atc_session import ATCSession
        from core.logic_manager import LogicManager

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = os.path.join(tmp.name, "lnm.sqlite")
        _build_lnm_db(db_path)
        svc = ProcedureService({"navdata": {"procedure_source": "lnm",
                                            "sqlite_path": db_path}})

        cfg = {"frequencies": {}, "audio": {"stt_language": "zh"}, "debug": {}}

        class _IO:
            def emit(self, event, data=None, *a, **k):
                pass

        session = ATCSession(cfg, None)
        lm = LogicManager(cfg, _IO(), atc_session=session, procedure_service=svc)
        lm.log_file = "NUL"
        # SimBrief 截断行为：VIBOS2A → VIBOS2
        lm.on_flight_plan_loaded({"origin": "ZGGG", "destination": "ZGGG",
                                  "cruise_alt": 30000, "sid": "VIBOS2",
                                  "star": "NYB"})
        session.advance_to("CLEARANCE")
        lm._update_issued_instructions([], "CCA1024, cleared to ZGGG via VIBOS2A.")
        self.assertEqual(session.get("sid"), "VIBOS2A")
        # 值必须与本地库一致（不是截断版 VIBOS2）
        entry = session.get_entry("sid")
        self.assertEqual(entry["value"], "VIBOS2A")

    def test_no_flight_plan_ident_never_blindly_assigns(self):
        from core.atc_session import ATCSession
        from core.logic_manager import LogicManager

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = os.path.join(tmp.name, "lnm.sqlite")
        _build_lnm_db(db_path)
        svc = ProcedureService({"navdata": {"procedure_source": "lnm",
                                            "sqlite_path": db_path}})
        cfg = {"frequencies": {}, "audio": {"stt_language": "zh"}, "debug": {}}

        class _IO:
            def emit(self, event, data=None, *a, **k):
                pass

        session = ATCSession(cfg, None)
        lm = LogicManager(cfg, _IO(), atc_session=session, procedure_service=svc)
        lm.log_file = "NUL"
        lm.on_flight_plan_loaded({"origin": "ZGGG", "destination": "ZGGG"})
        session.advance_to("CLEARANCE")
        lm._update_issued_instructions([], "CCA1024, cleared to ZGGG.")
        # 飞行计划里没有 sid 标识时不许盲选库中第一条
        self.assertIsNone(session.get("sid"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
