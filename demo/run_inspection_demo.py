"""外观抽检联合证书端到端演示：两次送审不同结果 + 争用/循环/离线重开。

剧情（全部真实落库、真实断言）
==============================
1. 目录发布外观抽检配置，解释候补关系：甲乙两台的候补都指向同一锚点的
   标准样声明 => 同一身份，只有一份依据；
2. 送审 sub-1（stations_v1.csv 全缺测）：甲乙结论都追溯到同一块标准样，
   光泽/亮度争用同一份依据 => 拒签，报告给出争用组；
3. 循环/全缺配置送审：x↔y 循环、gamma 全缺 => 报告列出无有效结论的格；
4. 后到的独立测量（stations_v2.csv：乙台光泽本机实测 7）重新送审 sub-2：
   光泽配乙（实测）、亮度配甲（标准样），依据不同 => 签发联合证书；
5. 采购单 PO-2026-002 规定光泽也只能用甲 => 两项争用同一标准样，拒签；
6. 源站离线（CSV 移走）后重开证书仓：证书自包含可回查、摘要可校验，
   同一采购单的送审历史（拒 -> 签）完整；
7. 兼容性：同一产品文档的 ingest: 段仍驱动经典 CSV 导入，行为不变。

运行：``python3 demo/run_inspection_demo.py``
产物：``data/demo_inspect_catalog.db`` / ``demo_inspect_tasks.db`` / ``demo_inspect_certs.db``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from confcat.catalog import Catalog
from confcat.certificate import CertificateAuthority, CertificateRepository
from confcat.ingest import TaskRepository

CATALOG_DB = ROOT / "data" / "demo_inspect_catalog.db"
TASK_DB = ROOT / "data" / "demo_inspect_tasks.db"
CERT_DB = ROOT / "data" / "demo_inspect_certs.db"
for f in (CATALOG_DB, TASK_DB, CERT_DB):
    f.unlink(missing_ok=True)

CSV_V1 = ROOT / "data" / "stations_v1.csv"
CSV_V2 = ROOT / "data" / "stations_v2.csv"
CSV_CYC = ROOT / "data" / "stations_cyclic.csv"


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def k(tok: dict) -> str:
    """类型化键 token -> 展示文本。"""
    return f"{tok['k']}({tok['kt']})" if tok["kt"] == "i" else str(tok["k"])


def show_chain(chain: list[dict]) -> str:
    parts = []
    for hop in chain:
        if hop["hop"] == "local":
            parts.append(f"本机实测列 {hop['column']}（任务 {hop['task']} 行{hop['row']}，原值 {hop['raw']!r}）")
        elif hop["hop"] == "cell":
            parts.append(f"引用原测格 {k(hop['station'])}.{k(hop['metric'])}")
        else:
            parts.append(f"标准样 {hop.get('id')}（声明 {hop['identity']}，值 {hop['value']}）")
    return " -> ".join(parts)


def main() -> int:
    hr("1) 发布外观抽检配置，目录解释候补关系")
    with Catalog(CATALOG_DB) as cat:
        cat.publish_file("appearance", ROOT / "samples" / "appearance_config.yaml", doc_names=["inspect"])
        cat.publish_file("appearance-cyclic", ROOT / "samples" / "appearance_cyclic.yaml", doc_names=["inspect"])
        with TaskRepository(TASK_DB, cat) as tasks, CertificateRepository(CERT_DB) as certs:
            auth = CertificateAuthority(cat, tasks, certs)
            graph_doc = auth.explain_fallbacks("appearance", "inspect")
            std_identities = set()
            for cell in graph_doc["cells"]:
                who = f"{k(cell['cell']['station'])}.{k(cell['cell']['metric'])}"
                rules = "；".join(
                    f"候补{r['index']+1}="
                    + (f"引用{k(r['target']['station'])}.{k(r['target']['metric'])}" if r["kind"] == "cell"
                       else f"标准样{r['standard']['id']}@{r['standard']['identity']}")
                    for r in cell["ordered_fallbacks"]
                )
                print(f"  格 {who:<18} 本机列={cell['local_column'] or '-':<10} {rules}")
                for r in cell["ordered_fallbacks"]:
                    if r["kind"] == "standard":
                        std_identities.add(r["standard"]["identity"])
            assert len(std_identities) == 1, "四个格的标准样候补应指向同一声明"
            print("  => 四个格的标准样候补身份相同：", std_identities.pop(), "（同一声明，只算一份依据）")

            hr("2) 兼容性：同一产品文档仍驱动经典 CSV 导入")
            s = tasks.create_task("classic-import", "appearance", "inspect", CSV_V1)
            print("  经典接入任务", s.task_id, "列:", s.columns, "行:", repo_rows := tasks.get_rows("classic-import"))
            assert repo_rows == [{"a_gloss": "NA", "a_bright": "NA", "b_gloss": "NA", "b_bright": "NA"}]

            hr("3) 送审 sub-1：两台全缺测，结论都追溯到同一块标准样 => 拒签")
            order1 = (ROOT / "samples" / "order_po001.yaml").read_text(encoding="utf-8")
            rep1 = auth.submit("sub-1", "appearance", "inspect", order1, CSV_V1)
            print("  结果:", rep1["result"], "/", rep1["reason"])
            for req in rep1["requirements"]:
                for c in req["candidates"]:
                    print(f"    候选 {req['metric'] and k(req['metric'])} <- {k(c['station'])}: "
                          f"值 {c['value']} 依据 {c['evidence']}")
            conflict = rep1["conflict"]
            print("  争用组:", [k(m) for m in conflict["requirements"]])
            print("  争用依据:", conflict["contested_evidence"])
            print("  说明:", conflict["detail"])
            assert rep1["result"] == "rejected" and rep1["reason"] == "evidence-conflict"
            assert {m["k"] for m in conflict["requirements"]} == {"gloss", "brightness"}
            assert len(conflict["contested_evidence"]) == 1

            hr("4) 循环/全缺：哪些格没有有效结论，送审报告可查")
            order3 = (ROOT / "samples" / "order_cyclic.yaml").read_text(encoding="utf-8")
            rep_cyc = auth.submit("sub-cyc", "appearance-cyclic", "inspect", order3, CSV_CYC)
            print("  结果:", rep_cyc["result"], "/", rep_cyc["reason"])
            for cell in rep_cyc["unresolved_cells"]:
                print(f"    无有效结论: {k(cell['cell']['station'])}.{k(cell['cell']['metric'])}"
                      f"  原因: {cell['reasons']}")
            assert rep_cyc["reason"] == "no-qualified-conclusion"
            reasons = {k(c["cell"]["station"]): c["reasons"] for c in rep_cyc["unresolved_cells"]}
            assert set(reasons) == {"x", "y", "gamma"}
            assert any("cycle" in r for r in reasons["x"]) and any("cycle" in r for r in reasons["y"])
            assert reasons["gamma"] == ["missing-token"]

            hr("5) 后到的独立测量重新送审 sub-2：乙台光泽本机实测 7 => 签发")
            order1_again = (ROOT / "samples" / "order_po001.yaml").read_text(encoding="utf-8")
            rep2 = auth.submit("sub-2", "appearance", "inspect", order1_again, CSV_V2)
            print("  结果:", rep2["result"], " 证书:", rep2.get("cert_id"))
            for a in rep2["assignment"]:
                print(f"    {k(a['metric'])} <- {k(a['station'])}: 值 {a['value']} 范围 {a['range']}"
                      f"\n        依据 {a['evidence']}\n        引用链 {show_chain(a['chain'])}")
            evs = [a["evidence"] for a in rep2["assignment"]]
            assert rep2["result"] == "issued" and len(set(evs)) == 2
            by_metric = {k(a["metric"]): a for a in rep2["assignment"]}
            assert k(by_metric["gloss"]["station"]) == "beta"
            assert k(by_metric["brightness"]["station"]) == "alpha"
            assert by_metric["gloss"]["source_kind"] == "measurement"
            assert by_metric["brightness"]["source_kind"] == "standard"

            hr("6) 采购单 PO-2026-002 规定光泽也只能用甲 => 争用同一标准样，拒签")
            order2 = (ROOT / "samples" / "order_po002.yaml").read_text(encoding="utf-8")
            rep3 = auth.submit("sub-3", "appearance", "inspect", order2, CSV_V2)
            print("  结果:", rep3["result"], "/", rep3["reason"])
            print("  争用组:", [k(m) for m in rep3["conflict"]["requirements"]],
                  " 争用依据:", rep3["conflict"]["contested_evidence"])
            assert rep3["result"] == "rejected" and rep3["reason"] == "evidence-conflict"

    hr("7) 源站离线：移走 CSV 后重开证书仓，证书自包含可回查校验")
    backups = {}
    for p in (CSV_V1, CSV_V2, CSV_CYC):
        bak = p.with_suffix(".csv.bak")
        p.rename(bak)
        backups[p] = bak
    try:
        assert not CSV_V2.exists(), "源站 CSV 已离线"
        with CertificateRepository(CERT_DB) as certs2:  # 只开证书仓，不碰目录/CSV
            cert = certs2.get_certificate("cert-sub-2")
            print("  离线重开证书:", cert["cert_id"], " 订单:", cert["order_id"],
                  " 校验:", certs2.verify_certificate("cert-sub-2"))
            for a in cert["payload"]["assignments"]:
                print(f"    {k(a['metric'])} <- {k(a['station'])}: 值 {a['value']}"
                      f"  依据 {a['evidence']}")
            print("  依据台账（去重后）:", json.dumps(cert["payload"]["evidence"],
                                                   ensure_ascii=False, indent=2))
            hist = certs2.list_submissions(order_id="PO-2026-001")
            print("  PO-2026-001 送审历史:", [(h["submission_id"], h["result"]) for h in hist])
            assert certs2.verify_certificate("cert-sub-2") == "ok"
            assert [h["result"] for h in hist] == ["rejected", "issued"]
            assert len(cert["payload"]["evidence"]) == 2, "两份独立依据，台账各一条"
    finally:
        for p, bak in backups.items():
            bak.rename(p)

    hr("全部演示断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
