"""外观抽检联合证书测试：候补图/解析引擎/评估匹配/送审持久化/兼容性。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from confcat.catalog import Catalog
from confcat.certificate import (
    CertificateAuthority,
    CertificateRepository,
    evaluate,
    parse_order,
)
from confcat.errors import InspectionError, TaskRejected
from confcat.fallback import FallbackGraph, Resolver, flatten_reasons, parse_measurement
from confcat.ingest import TaskRepository
from confcat.materialize import materialize_all
from confcat.yaml_parser import parse_text

# --------------------------------------------------------------------------- #
# 样例配置
# --------------------------------------------------------------------------- #
APPEARANCE = """
standards:
  gray_plate: &gray_plate
    id: STD-GRAY-01
    value: 7
stations:
  alpha:
    label: 甲
    columns:
      gloss: a_gloss
      brightness: a_bright
    fallback:
      gloss:
        - standard: *gray_plate
      brightness:
        - standard: *gray_plate
  beta:
    label: 乙
    columns:
      gloss: b_gloss
      brightness: b_bright
    fallback:
      gloss:
        - cell:
            station: alpha
            metric: gloss
        - standard: *gray_plate
      brightness:
        - cell:
            station: alpha
            metric: brightness
        - standard: *gray_plate
ingest:
  source_uri: file:///data/stations.csv
  column_mapping:
    a_gloss: a_gloss
    a_bright: a_bright
    b_gloss: b_gloss
    b_bright: b_bright
  missing_value:
    token: "NA"
"""

ORDER_BOTH = """
order: PO-1
requirements:
  - metric: gloss
    stations:
      - alpha
      - beta
    min: 5
    max: 9
  - metric: brightness
    stations:
      - alpha
    min: 5
    max: 9
"""

ORDER_ALPHA_ONLY = """
order: PO-2
requirements:
  - metric: gloss
    stations:
      - alpha
    min: 5
    max: 9
  - metric: brightness
    stations:
      - alpha
    min: 5
    max: 9
"""

CSV_ALL_MISSING = "a_gloss,a_bright,b_gloss,b_bright\nNA,NA,NA,NA\n"
CSV_BETA_GLOSS = "a_gloss,a_bright,b_gloss,b_bright\nNA,NA,7,NA\n"


def graph_of(yaml_text: str) -> FallbackGraph:
    return FallbackGraph.from_document(materialize_all(parse_text(yaml_text))[0])


def resolver_of(yaml_text: str, measurements: dict, token: str = "NA") -> Resolver:
    r = Resolver(graph_of(yaml_text), measurements, missing_token=token, meas_ns="t1")
    r.resolve_all()
    return r


# --------------------------------------------------------------------------- #
# 候补关系图
# --------------------------------------------------------------------------- #
class FallbackGraphTest(unittest.TestCase):
    def test_extract_cells_columns_rules(self):
        g = graph_of(APPEARANCE)
        self.assertEqual(
            set(g.cells()),
            {
                ("alpha", "gloss"),
                ("alpha", "brightness"),
                ("beta", "gloss"),
                ("beta", "brightness"),
            },
        )
        self.assertEqual(g.column_of(("alpha", "gloss")), "a_gloss")
        rules = g.rules_of(("beta", "gloss"))
        self.assertEqual([r.kind for r in rules], ["cell", "standard"])
        self.assertEqual(rules[0].target, ("alpha", "gloss"))
        self.assertEqual(rules[1].standard_value, 7)
        self.assertEqual(rules[1].standard_id, "STD-GRAY-01")

    def test_shared_alias_one_identity(self):
        """不同路径用别名引用同一标准样声明 => 身份相同（只有一份依据）。"""
        g = graph_of(APPEARANCE)
        ids = {
            r.standard_identity
            for cell in g.cells()
            for r in g.rules_of(cell)
            if r.kind == "standard"
        }
        self.assertEqual(len(ids), 1)
        self.assertTrue(ids.pop().startswith("std:gray_plate@"))

    def test_inline_declarations_are_distinct(self):
        """两处各自内联的同值声明是两份依据（文本独立才算独立）。"""
        yaml = (
            "stations:\n"
            "  a:\n    fallback:\n      gloss:\n"
            "        - standard:\n            value: 7\n"
            "  b:\n    fallback:\n      gloss:\n"
            "        - standard:\n            value: 7\n"
        )
        g = graph_of(yaml)
        id_a = g.rules_of(("a", "gloss"))[0].standard_identity
        id_b = g.rules_of(("b", "gloss"))[0].standard_identity
        self.assertNotEqual(id_a, id_b)
        self.assertTrue(id_a.startswith("std:inline@"))

    def test_typed_keys_int_vs_str_station(self):
        """台键 1(int) 与 "1"(str) 是两个台；依据 id 与引用链保留类型化键。"""
        yaml = (
            "stations:\n"
            "  1:\n    columns:\n      gloss: c_int\n"
            '  "1":\n    columns:\n      gloss: c_str\n'
        )
        g = graph_of(yaml)
        self.assertEqual(set(g.cells()), {(1, "gloss"), ("1", "gloss")})
        self.assertEqual(g.column_of((1, "gloss")), "c_int")
        self.assertEqual(g.column_of(("1", "gloss")), "c_str")
        r = Resolver(g, {"c_int": "5", "c_str": "6"}, meas_ns="t")
        self.assertEqual(r.resolve((1, "gloss")).evidence, "meas:t:i:1:s:gloss")
        self.assertEqual(r.resolve(("1", "gloss")).evidence, "meas:t:s:1:s:gloss")
        # 引用链中的类型化键 token
        hop_cell = r.resolve((1, "gloss")).chain[0]
        self.assertEqual(hop_cell["hop"], "local")

    def test_describe_explainable(self):
        g = graph_of(APPEARANCE)
        desc = g.describe()
        self.assertEqual(len(desc["cells"]), 4)
        beta_gloss = next(
            c for c in desc["cells"] if c["cell"]["station"]["k"] == "beta"
            and c["cell"]["metric"]["k"] == "gloss"
        )
        self.assertEqual(beta_gloss["local_column"], "b_gloss")
        self.assertEqual(
            [r["kind"] for r in beta_gloss["ordered_fallbacks"]], ["cell", "standard"]
        )
        self.assertEqual(
            beta_gloss["ordered_fallbacks"][1]["standard"]["id"], "STD-GRAY-01"
        )

    def test_bad_rule_rejected(self):
        with self.assertRaises(InspectionError):
            graph_of("stations:\n  a:\n    fallback:\n      gloss:\n        - bogus: 1\n")
        with self.assertRaises(InspectionError):
            graph_of(
                "stations:\n  a:\n    fallback:\n      gloss:\n"
                "        - cell:\n            station: b\n"
                "          standard:\n            value: 1\n"
            )
        with self.assertRaises(InspectionError):
            graph_of(
                "stations:\n  a:\n    fallback:\n      gloss:\n"
                "        - cell:\n            station: b\n"
            )
        with self.assertRaises(InspectionError):
            graph_of(
                "stations:\n  a:\n    fallback:\n      gloss:\n"
                '        - standard:\n            value: "7"\n'
            )
        with self.assertRaises(InspectionError):
            graph_of("stations:\n  a:\n    columns:\n      gloss: 7\n")


# --------------------------------------------------------------------------- #
# 解析引擎
# --------------------------------------------------------------------------- #
class ResolverTest(unittest.TestCase):
    def test_local_hit_skips_fallbacks(self):
        """本机实测命中：候补全部 not-reached，依据只有实测一份。"""
        r = resolver_of(APPEARANCE, {"a_gloss": "8", "b_gloss": "7"})
        res = r.resolve(("alpha", "gloss"))
        self.assertEqual(res.status, "resolved")
        self.assertEqual(res.value, 8)
        self.assertEqual(res.evidence, "meas:t1:s:alpha:s:gloss")
        self.assertEqual(res.source_kind, "measurement")
        self.assertEqual([s["reason"] for s in res.skipped], ["not-reached"])
        self.assertEqual(res.attempts, ())

    def test_cell_ref_shares_measurement_evidence(self):
        """乙引用甲的原测格：两格共用一次实测 => 依据 id 相同。"""
        r = resolver_of(APPEARANCE, {"a_gloss": "6"})
        beta = r.resolve(("beta", "gloss"))
        alpha = r.resolve(("alpha", "gloss"))
        self.assertEqual(beta.status, "resolved")
        self.assertEqual(beta.value, 6)
        self.assertEqual(beta.evidence, alpha.evidence)
        self.assertEqual(beta.evidence, "meas:t1:s:alpha:s:gloss")
        # 引用链：乙 -> cell 甲 -> 本机列
        self.assertEqual([h["hop"] for h in beta.chain], ["cell", "local"])
        self.assertEqual(beta.chain[0]["station"], {"kt": "s", "k": "alpha"})
        # 乙自己的标准样候补未走到，不占用依据
        self.assertEqual([s["reason"] for s in beta.skipped], ["not-reached"])

    def test_shared_standard_one_evidence(self):
        r = resolver_of(APPEARANCE, {})
        cells = [("alpha", "gloss"), ("alpha", "brightness"), ("beta", "gloss")]
        evidences = {r.resolve(c).evidence for c in cells}
        self.assertEqual(len(evidences), 1)
        self.assertTrue(evidences.pop().startswith("std:gray_plate@"))

    def test_cycle_and_all_missing_queryable(self):
        yaml = (
            "stations:\n"
            "  x:\n    columns:\n      gloss: xg\n"
            "    fallback:\n      gloss:\n"
            "        - cell:\n            station: y\n            metric: gloss\n"
            "  y:\n    columns:\n      gloss: yg\n"
            "    fallback:\n      gloss:\n"
            "        - cell:\n            station: x\n            metric: gloss\n"
            "  z:\n    columns:\n      gloss: zg\n"
        )
        r = resolver_of(yaml, {"xg": "NA", "yg": "NA", "zg": "NA"})
        unresolved = r.unresolved()
        self.assertEqual(set(unresolved), {("x", "gloss"), ("y", "gloss"), ("z", "gloss")})
        self.assertTrue(any("cycle" in s for s in flatten_reasons(unresolved[("x", "gloss")])))
        self.assertTrue(any("cycle" in s for s in flatten_reasons(unresolved[("y", "gloss")])))
        self.assertEqual(flatten_reasons(unresolved[("z", "gloss")]), ["missing-token"])

    def test_cycle_with_exit_still_resolves(self):
        """第一候补成环失败，第二候补标准样仍让该格有有效结论。"""
        yaml = (
            "standards:\n  p: &p\n    value: 7\n"
            "stations:\n"
            "  x:\n    fallback:\n      gloss:\n"
            "        - cell:\n            station: y\n            metric: gloss\n"
            "        - standard: *p\n"
            "  y:\n    fallback:\n      gloss:\n"
            "        - cell:\n            station: x\n            metric: gloss\n"
        )
        r = resolver_of(yaml, {})
        x = r.resolve(("x", "gloss"))
        self.assertEqual(x.status, "resolved")
        self.assertEqual(x.source_kind, "standard")
        self.assertTrue(any("cycle" in s for s in flatten_reasons(x)))
        # y 只能经 x，最终也落到同一块标准样 => 与 x 共用一份依据
        y = r.resolve(("y", "gloss"))
        self.assertEqual(y.status, "resolved")
        self.assertEqual(y.evidence, x.evidence)

    def test_memoization_not_poisoned_by_cycle(self):
        """X 先经环失败再走标准样；之后单独解析 Y 必须重新得到 X 的结论。"""
        yaml = (
            "standards:\n  p: &p\n    value: 7\n"
            "stations:\n"
            "  x:\n    fallback:\n      gloss:\n"
            "        - cell:\n            station: y\n            metric: gloss\n"
            "        - standard: *p\n"
            "  y:\n    fallback:\n      gloss:\n"
            "        - cell:\n            station: x\n            metric: gloss\n"
        )
        r = Resolver(graph_of(yaml), {}, meas_ns="t")
        x = r.resolve(("x", "gloss"))
        self.assertEqual(x.status, "resolved")
        y = r.resolve(("y", "gloss"))  # 若错误记忆了“Y 不可解”这里会露馅
        self.assertEqual(y.status, "resolved")
        self.assertEqual(y.evidence, x.evidence)

    def test_parse_measurement_int_contract(self):
        self.assertEqual(parse_measurement("7"), 7)
        self.assertEqual(parse_measurement("-7"), -7)
        self.assertEqual(parse_measurement("0"), 0)
        self.assertIsNone(parse_measurement("007"))
        self.assertIsNone(parse_measurement("7.5"))
        self.assertIsNone(parse_measurement(""))


# --------------------------------------------------------------------------- #
# 采购单解析与评估
# --------------------------------------------------------------------------- #
class OrderParseTest(unittest.TestCase):
    def test_parse_ok(self):
        order = parse_order(ORDER_BOTH)
        self.assertEqual(order.order_id, "PO-1")
        self.assertEqual(order.requirements[0].stations, ("alpha", "beta"))
        self.assertEqual((order.requirements[0].min, order.requirements[0].max), (5, 9))

    def test_bad_orders_rejected(self):
        with self.assertRaises(InspectionError):
            parse_order("requirements:\n  - metric: gloss\n    stations:\n      - a\n    min: 1\n    max: 2\n")
        with self.assertRaises(InspectionError):
            parse_order("order: PO\nrequirements:\n  - metric: gloss\n    stations:\n      - a\n    min: 9\n    max: 5\n")
        with self.assertRaises(InspectionError):  # 重复指标
            parse_order(
                "order: PO\nrequirements:\n"
                "  - metric: gloss\n    stations:\n      - a\n    min: 1\n    max: 2\n"
                "  - metric: gloss\n    stations:\n      - b\n    min: 1\n    max: 2\n"
            )
        with self.assertRaises(InspectionError):  # 范围必须 int
            parse_order(
                "order: PO\nrequirements:\n"
                '  - metric: gloss\n    stations:\n      - a\n    min: "1"\n    max: 2\n'
            )


class EvaluateTest(unittest.TestCase):
    def test_issue_with_distinct_evidence(self):
        r = resolver_of(APPEARANCE, {"b_gloss": "7"})
        report = evaluate(parse_order(ORDER_BOTH), graph_of(APPEARANCE), r)
        self.assertEqual(report["result"], "issued")
        by_metric = {a["metric"]["k"]: a for a in report["assignment"]}
        self.assertEqual(by_metric["gloss"]["station"]["k"], "beta")
        self.assertEqual(by_metric["brightness"]["station"]["k"], "alpha")
        evs = [a["evidence"] for a in report["assignment"]]
        self.assertEqual(len(set(evs)), 2)

    def test_conflict_on_shared_standard(self):
        r = resolver_of(APPEARANCE, {})
        report = evaluate(parse_order(ORDER_BOTH), graph_of(APPEARANCE), r)
        self.assertEqual(report["result"], "rejected")
        self.assertEqual(report["reason"], "evidence-conflict")
        conflict = report["conflict"]
        self.assertEqual({m["k"] for m in conflict["requirements"]}, {"gloss", "brightness"})
        self.assertEqual(len(conflict["contested_evidence"]), 1)

    def test_minimal_conflict_set_excludes_independent_metric(self):
        """三指标两依据：极小争用组只含真正争用的两个指标。"""
        yaml = (
            "standards:\n  p: &p\n    value: 7\n"
            "stations:\n"
            "  a:\n    fallback:\n      m1:\n        - standard: *p\n"
            "      m2:\n        - standard: *p\n"
            "      m3:\n        - standard: *p\n"
            "  b:\n    columns:\n      m3: bm3\n"
        )
        order = parse_order(
            "order: PO\nrequirements:\n"
            "  - metric: m1\n    stations:\n      - a\n    min: 5\n    max: 9\n"
            "  - metric: m2\n    stations:\n      - a\n    min: 5\n    max: 9\n"
            "  - metric: m3\n    stations:\n      - a\n      - b\n    min: 5\n    max: 9\n"
        )
        g = graph_of(yaml)
        r = Resolver(g, {"bm3": "8"}, meas_ns="t")
        r.resolve_all()
        report = evaluate(order, g, r)
        self.assertEqual(report["reason"], "evidence-conflict")
        # m3 有独立实测可让路，极小争用组是 {m1, m2}
        self.assertEqual({m["k"] for m in report["conflict"]["requirements"]}, {"m1", "m2"})
        self.assertEqual(len(report["conflict"]["contested_evidence"]), 1)

    def test_unqualified_out_of_range_and_missing(self):
        r = resolver_of(APPEARANCE, {"b_gloss": "20"})  # 超范围；alpha 全缺 -> 标准样 7
        order = parse_order(
            "order: PO\nrequirements:\n"
            "  - metric: gloss\n    stations:\n      - beta\n    min: 5\n    max: 9\n"
        )
        report = evaluate(order, graph_of(APPEARANCE), r)
        self.assertEqual(report["result"], "rejected")
        self.assertEqual(report["reason"], "no-qualified-conclusion")
        opts = report["unqualified"][0]["options"]
        self.assertEqual(opts[0]["reason"], "out-of-range")

    def test_non_numeric_measurement_not_qualified(self):
        r = resolver_of(APPEARANCE, {"b_gloss": "7.5"})
        order = parse_order(
            "order: PO\nrequirements:\n"
            "  - metric: gloss\n    stations:\n      - beta\n    min: 5\n    max: 9\n"
        )
        report = evaluate(order, graph_of(APPEARANCE), r)
        self.assertEqual(report["unqualified"][0]["options"][0]["reason"], "non-numeric")


# --------------------------------------------------------------------------- #
# 送审与证书仓（集成：真实 SQLite + CSV）
# --------------------------------------------------------------------------- #
class AuthorityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cat_db, self.task_db, self.cert_db = d / "cat.db", d / "task.db", d / "cert.db"
        self.csv_v1 = d / "v1.csv"
        self.csv_v2 = d / "v2.csv"
        self.csv_v1.write_text(CSV_ALL_MISSING, encoding="utf-8")
        self.csv_v2.write_text(CSV_BETA_GLOSS, encoding="utf-8")
        self.cat = Catalog(self.cat_db)
        self.cat.publish("appearance", APPEARANCE, doc_names=["inspect"])
        self.tasks = TaskRepository(self.task_db, self.cat)
        self.certs = CertificateRepository(self.cert_db)
        self.auth = CertificateAuthority(self.cat, self.tasks, self.certs)

    def tearDown(self):
        self.certs.close()
        self.tasks.close()
        self.cat.close()
        self.tmp.cleanup()

    def test_two_submissions_different_results(self):
        """题面主线：同一张采购单，全缺测拒签；后到独立测量重审签发。"""
        rep1 = self.auth.submit("s1", "appearance", "inspect", ORDER_BOTH, self.csv_v1)
        self.assertEqual((rep1["result"], rep1["reason"]), ("rejected", "evidence-conflict"))
        self.assertEqual(len(rep1["conflict"]["contested_evidence"]), 1)

        rep2 = self.auth.submit("s2", "appearance", "inspect", ORDER_BOTH, self.csv_v2)
        self.assertEqual(rep2["result"], "issued")
        evs = [a["evidence"] for a in rep2["assignment"]]
        self.assertEqual(len(set(evs)), 2)

        hist = self.certs.list_submissions(order_id="PO-1")
        self.assertEqual([h["result"] for h in hist], ["rejected", "issued"])

    def test_alpha_only_order_conflicts(self):
        rep = self.auth.submit("s1", "appearance", "inspect", ORDER_ALPHA_ONLY, self.csv_v2)
        self.assertEqual((rep["result"], rep["reason"]), ("rejected", "evidence-conflict"))

    def test_certificate_offline_reopen(self):
        """已发证书在源站离线（CSV 删除）后可重开、可校验。"""
        rep = self.auth.submit("s1", "appearance", "inspect", ORDER_BOTH, self.csv_v2)
        cert_id = rep["cert_id"]
        self.certs.close()
        self.tasks.close()
        self.cat.close()
        self.csv_v2.unlink()  # 源站离线
        with CertificateRepository(self.cert_db) as certs2:
            cert = certs2.get_certificate(cert_id)
            self.assertEqual(cert["order_id"], "PO-1")
            self.assertEqual(certs2.verify_certificate(cert_id), "ok")
            self.assertEqual(len(cert["payload"]["evidence"]), 2)
            kinds = {e["kind"] for e in cert["payload"]["evidence"].values()}
            self.assertEqual(kinds, {"measurement", "standard"})
        # 重新打开供 tearDown 关闭
        self.cat = Catalog(self.cat_db)
        self.tasks = TaskRepository(self.task_db, self.cat)
        self.certs = CertificateRepository(self.cert_db)

    def test_identity_stable_across_catalog_reopen(self):
        """标准样身份来自落库的来源证据：目录重开后身份不变。"""
        before = self.auth.explain_fallbacks("appearance", "inspect")
        self.cat.close()
        self.cat = Catalog(self.cat_db)
        self.auth = CertificateAuthority(self.cat, self.tasks, self.certs)
        after = self.auth.explain_fallbacks("appearance", "inspect")
        ids_before = {
            r["standard"]["identity"]
            for c in before["cells"]
            for r in c["ordered_fallbacks"]
            if r["kind"] == "standard"
        }
        ids_after = {
            r["standard"]["identity"]
            for c in after["cells"]
            for r in c["ordered_fallbacks"]
            if r["kind"] == "standard"
        }
        self.assertEqual(ids_before, ids_after)
        self.assertEqual(len(ids_before), 1)

    def test_duplicate_submission_rejected(self):
        self.auth.submit("s1", "appearance", "inspect", ORDER_BOTH, self.csv_v1)
        with self.assertRaises(InspectionError) as ctx:
            self.auth.submit("s1", "appearance", "inspect", ORDER_BOTH, self.csv_v1)
        self.assertEqual(ctx.exception.reason, "duplicate-submission")

    def test_malformed_order_leaves_no_record(self):
        with self.assertRaises(InspectionError):
            self.auth.submit("s1", "appearance", "inspect", "order: 123\n", self.csv_v1)
        self.assertEqual(self.certs.list_submissions(), [])
        self.assertEqual(self.tasks.list_tasks(), [])

    def test_bad_csv_leaves_no_record(self):
        self.csv_v1.write_text("a_gloss\nNA\n", encoding="utf-8")  # 缺原列
        with self.assertRaises(TaskRejected):
            self.auth.submit("s1", "appearance", "inspect", ORDER_BOTH, self.csv_v1)
        self.assertEqual(self.certs.list_submissions(), [])

    def test_classic_ingest_compatibility(self):
        """原 CSV 导入合同不变：同一产品文档可直接跑经典接入任务。"""
        summary = self.tasks.create_task("classic", "appearance", "inspect", self.csv_v2)
        self.assertEqual(summary.columns, ("a_gloss", "a_bright", "b_gloss", "b_bright"))
        rows = self.tasks.get_rows("classic")
        self.assertEqual(rows[0]["b_gloss"], "7")
        self.assertEqual(rows[0]["a_gloss"], "NA")  # 无 default，保留缺失 token

    def test_typed_station_keys_end_to_end(self):
        """订单用 int 台键寻址 int 台、str 台键寻址 str 台，互不相同。"""
        yaml = (
            "stations:\n"
            "  1:\n    columns:\n      gloss: g_int\n      brightness: b_int\n"
            '  "1":\n    columns:\n      gloss: g_str\n      brightness: b_str\n'
            "ingest:\n  source_uri: file:///x.csv\n"
            "  column_mapping:\n"
            "    g_int: g_int\n    b_int: b_int\n    g_str: g_str\n    b_str: b_str\n"
        )
        order = (
            "order: PO-T\nrequirements:\n"
            "  - metric: gloss\n    stations:\n      - 1\n    min: 5\n    max: 9\n"
            '  - metric: brightness\n    stations:\n      - "1"\n    min: 5\n    max: 9\n'
        )
        self.cat.publish("typed", yaml, doc_names=["inspect"])
        csv = Path(self.tmp.name) / "typed.csv"
        csv.write_text("g_int,b_int,g_str,b_str\n7,NA,NA,8\n", encoding="utf-8")
        rep = self.auth.submit("st", "typed", "inspect", order, csv)
        self.assertEqual(rep["result"], "issued")
        by_metric = {a["metric"]["k"]: a for a in rep["assignment"]}
        self.assertEqual(by_metric["gloss"]["station"], {"kt": "i", "k": 1})
        self.assertEqual(by_metric["brightness"]["station"], {"kt": "s", "k": "1"})
        self.assertIn("i:1", by_metric["gloss"]["evidence"])
        self.assertIn("s:1", by_metric["brightness"]["evidence"])


if __name__ == "__main__":
    unittest.main()
