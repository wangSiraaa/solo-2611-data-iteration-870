"""外观指标联合证书：采购单评估、依据独立性匹配、签发与持久化。

签发合同
========
* 采购单（受限 YAML）逐项声明 ``metric`` / 可接纳检测台 ``stations`` /
  合格范围 ``[min, max]``（闭区间，int）；
* 每项指标从可接纳台中选一个“有有效结论且值在范围内”的格作为结论；
* **任意两项指标的最终依据必须不同**——依据即解析引擎给出的实测格身份或
  标准样声明身份；该约束归约为“指标 -> 依据”的二部匹配（Kuhn 增广路）；
* 不能签时给出可复核原因：
    - ``no-qualified-conclusion``：自身无合格结论的指标（逐台给出原因）；
    - ``evidence-conflict``：互相争用的最小指标组 + 该组实际可用的依据清单
      （删除法求极小不可满足子集，组内依据数 < 指标数）；
* 送审记录与证书**自包含**落 SQLite：源 CSV/源站离线后仍可重开证书、
  校验摘要；后到的独立测量用新 CSV 重新送审，同一 order 的历史可回查。

与既有仓的关系
==============
CSV 接入完全复用 :class:`confcat.ingest.TaskRepository`（同一产品文档内的
``ingest:`` 段），本模块只新增送审/证书两张表，不改变原有任务合同。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .errors import InspectionError
from .fallback import FallbackGraph, Resolver, flatten_reasons, key_token
from .ingest import TaskRepository
from .materialize import materialize_all
from .model import ConfigKey, MatMapping, MatScalar, MatSequence
from .yaml_parser import parse_text

# --------------------------------------------------------------------------- #
# 采购单
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Requirement:
    metric: ConfigKey
    stations: tuple  # 可接纳检测台（类型化键，有序）
    min: int
    max: int

    def as_dict(self) -> dict:
        return {
            "metric": key_token(self.metric),
            "stations": [key_token(s) for s in self.stations],
            "min": self.min,
            "max": self.max,
        }


@dataclass(frozen=True)
class Order:
    order_id: str
    requirements: tuple[Requirement, ...]

    def as_dict(self) -> dict:
        return {"order": self.order_id, "requirements": [r.as_dict() for r in self.requirements]}


def _req_scalar(node, path, need: str):
    v = node.entries.get(need)
    if v is None or not isinstance(v, MatScalar):
        raise InspectionError(f"采购单要求缺少 {need}", path=path + (need,), reason="bad-order")
    return v


def parse_order(yaml_text: str) -> Order:
    """解析并校验采购单；结构/类型违规抛 :class:`InspectionError`（不留记录）。"""
    docs = materialize_all(parse_text(yaml_text))
    if len(docs) != 1:
        raise InspectionError("采购单必须是单文档 YAML", reason="bad-order")
    root = docs[0].root
    if not isinstance(root, MatMapping):
        raise InspectionError("采购单根必须是映射", reason="bad-order")
    id_node = root.entries.get("order")
    if id_node is None or not isinstance(id_node, MatScalar) or not isinstance(id_node.value, str):
        raise InspectionError("采购单缺少字符串 order 编号", path=("order",), reason="bad-order")
    reqs_node = root.entries.get("requirements")
    if reqs_node is None or not isinstance(reqs_node, MatSequence) or not reqs_node.items:
        raise InspectionError("采购单 requirements 必须是非空序列", path=("requirements",), reason="bad-order")

    requirements: list[Requirement] = []
    seen_metrics: set = set()
    for idx, item in enumerate(reqs_node.items):
        path = ("requirements", idx)
        if not isinstance(item, MatMapping):
            raise InspectionError("每项要求必须是映射", path=path, reason="bad-order")
        metric_node = _req_scalar(item, path, "metric")
        if isinstance(metric_node.value, bool) or not isinstance(metric_node.value, (str, int)):
            raise InspectionError("metric 必须是 str/int 标量", path=path + ("metric",), reason="bad-order")
        metric = metric_node.value
        sig = (type(metric), metric)
        if sig in seen_metrics:
            raise InspectionError(f"指标 {metric!r} 重复要求", path=path + ("metric",), reason="bad-order")
        seen_metrics.add(sig)

        st_node = item.entries.get("stations")
        if st_node is None or not isinstance(st_node, MatSequence) or not st_node.items:
            raise InspectionError("stations 必须是非空序列", path=path + ("stations",), reason="bad-order")
        stations: list[ConfigKey] = []
        for s_idx, s in enumerate(st_node.items):
            if not isinstance(s, MatScalar) or isinstance(s.value, bool) or not isinstance(s.value, (str, int)):
                raise InspectionError(
                    "stations 项必须是 str/int 标量",
                    path=path + ("stations", s_idx),
                    reason="bad-order",
                )
            stations.append(s.value)

        bounds: list[int] = []
        for need in ("min", "max"):
            b = _req_scalar(item, path, need)
            if isinstance(b.value, bool) or not isinstance(b.value, int):
                raise InspectionError(f"{need} 必须是 int", path=path + (need,), reason="bad-order")
            bounds.append(b.value)
        if bounds[0] > bounds[1]:
            raise InspectionError("合格范围 min 不能大于 max", path=path, reason="bad-order")
        requirements.append(
            Requirement(metric=metric, stations=tuple(stations), min=bounds[0], max=bounds[1])
        )
    return Order(order_id=id_node.value, requirements=tuple(requirements))


# --------------------------------------------------------------------------- #
# 评估：候选 -> 匹配 -> 争用分析
# --------------------------------------------------------------------------- #
@dataclass
class Candidate:
    station: ConfigKey
    resolution: Any  # fallback.Resolution

    @property
    def evidence(self) -> str:
        return self.resolution.evidence


def _match(metrics: list, cand: dict) -> dict | None:
    """指标 -> 依据的二部匹配（每项指标取一个候选，依据两两不同）。

    返回 ``{metric: evidence}``；不可满足返回 None。确定性：候选按采购单
    stations 顺序、指标按给定顺序尝试。
    """
    owner: dict[str, Any] = {}  # evidence -> metric

    def augment(metric, seen: set) -> bool:
        for c in cand[metric]:
            e = c.evidence
            if e in seen:
                continue
            seen.add(e)
            if e not in owner or augment(owner[e], seen):
                owner[e] = metric
                return True
        return False

    for metric in metrics:
        if not augment(metric, set()):
            return None
    return {metric: ev for ev, metric in owner.items()}


def _minimal_conflict(metrics: list, cand: dict) -> list:
    """删除法求极小不可满足子集：组内任意真子集都可签，整组不可签。"""
    current = list(metrics)
    for m in list(current):
        trial = [x for x in current if x != m]
        if trial and _match(trial, cand) is None:
            current = trial
    return current


def evaluate(order: Order, graph: FallbackGraph, resolver: Resolver) -> dict:
    """评估采购单，产出可复核报告（不持久化，持久化由 Authority 负责）。"""
    resolver.resolve_all()  # 幂等（有记忆）；保证 unresolved() 覆盖全图
    cand: dict = {}
    req_reports: list[dict] = []
    unqualified: list[dict] = []

    for req in order.requirements:
        options: list[Candidate] = []
        rejected: list[dict] = []
        for station in req.stations:
            cell = (station, req.metric)
            if not graph.has_cell(cell):
                rejected.append({"station": key_token(station), "reason": "no-such-cell"})
                continue
            res = resolver.resolve(cell)
            if res.status != "resolved":
                rejected.append(
                    {
                        "station": key_token(station),
                        "reason": "unresolved",
                        "detail": flatten_reasons(res),
                    }
                )
                continue
            if res.value is None:
                rejected.append(
                    {
                        "station": key_token(station),
                        "reason": "non-numeric",
                        "detail": [f"raw={res.raw!r}"],
                    }
                )
                continue
            if not (req.min <= res.value <= req.max):
                rejected.append(
                    {
                        "station": key_token(station),
                        "reason": "out-of-range",
                        "detail": [f"value={res.value} not in [{req.min}, {req.max}]"],
                    }
                )
                continue
            options.append(Candidate(station=station, resolution=res))
        cand[req.metric] = options
        req_reports.append(
            {
                **req.as_dict(),
                "candidates": [
                    {
                        "station": key_token(c.station),
                        "value": c.resolution.value,
                        "evidence": c.evidence,
                        "source_kind": c.resolution.source_kind,
                    }
                    for c in options
                ],
                "rejected_options": rejected,
            }
        )
        if not options:
            unqualified.append({"metric": key_token(req.metric), "options": rejected})

    report: dict[str, Any] = {
        "order": order.as_dict(),
        "requirements": req_reports,
        "unresolved_cells": [
            {
                "cell": key_token_pair(cell),
                "reasons": flatten_reasons(r) or ["no-source"],
                "attempts": list(r.attempts),
            }
            for cell, r in resolver.unresolved().items()
        ],
    }

    if unqualified:
        report.update(
            {"result": "rejected", "reason": "no-qualified-conclusion", "unqualified": unqualified}
        )
        return report

    metrics = [r.metric for r in order.requirements]
    match = _match(metrics, cand)
    if match is None:
        conflict = _minimal_conflict(metrics, cand)
        contested = sorted({c.evidence for m in conflict for c in cand[m]})
        report.update(
            {
                "result": "rejected",
                "reason": "evidence-conflict",
                "conflict": {
                    "requirements": [key_token(m) for m in conflict],
                    "contested_evidence": contested,
                    "detail": (
                        f"指标 {[str(m) for m in conflict]} 的合格结论合计只能追溯到 "
                        f"{len(contested)} 份依据 {contested}，少于指标数 {len(conflict)}，"
                        "无法满足任意两项最终依据不同"
                    ),
                },
            }
        )
        return report

    # 签发：按采购单顺序给出每项指标的选用结论与完整引用链
    assignments = []
    for req in order.requirements:
        ev = match[req.metric]
        chosen = next(c for c in cand[req.metric] if c.evidence == ev)
        assignments.append(
            {
                "metric": key_token(req.metric),
                "station": key_token(chosen.station),
                "value": chosen.resolution.value,
                "raw": chosen.resolution.raw,
                "evidence": ev,
                "source_kind": chosen.resolution.source_kind,
                "chain": list(chosen.resolution.chain),
                "range": [req.min, req.max],
            }
        )
    report.update({"result": "issued", "reason": None, "assignment": assignments})
    return report


def key_token_pair(cell: tuple) -> dict:
    return {"station": key_token(cell[0]), "metric": key_token(cell[1])}


# --------------------------------------------------------------------------- #
# 送审/证书仓（SQLite，自包含，源站离线可重开）
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE TABLE IF NOT EXISTS submission (
    submission_id TEXT PRIMARY KEY,
    order_id      TEXT NOT NULL,
    product       TEXT NOT NULL,
    version       INTEGER NOT NULL,
    doc_name      TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    source_csv    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    result        TEXT NOT NULL,
    report_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS certificate (
    cert_id       TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL,
    order_id      TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    digest        TEXT NOT NULL,
    FOREIGN KEY (submission_id) REFERENCES submission(submission_id)
);
"""


class CertificateRepository:
    """送审记录与已发证书的持久化。重开只读本库，不触碰源 CSV/目录。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "CertificateRepository":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    def has_submission(self, submission_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM submission WHERE submission_id = ?", (submission_id,)
            ).fetchone()
            is not None
        )

    def record_submission(
        self,
        submission_id: str,
        order_id: str,
        product: str,
        version: int,
        doc_name: str,
        task_id: str,
        source_csv: str,
        result: str,
        report: dict,
        created_at: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO submission(submission_id, order_id, product, version, doc_name,"
            " task_id, source_csv, created_at, result, report_json)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                submission_id,
                order_id,
                product,
                version,
                doc_name,
                task_id,
                source_csv,
                created_at,
                result,
                json.dumps(report, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def issue_certificate(
        self, cert_id: str, submission_id: str, order_id: str, payload: dict, created_at: str
    ) -> str:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.conn.execute(
            "INSERT INTO certificate(cert_id, submission_id, order_id, created_at,"
            " payload_json, digest) VALUES(?, ?, ?, ?, ?, ?)",
            (cert_id, submission_id, order_id, created_at, text, digest),
        )
        self.conn.commit()
        return digest

    # ------------------------------------------------------------------ #
    def get_certificate(self, cert_id: str) -> dict:
        """离线重开：payload 自包含（结论、依据、引用链、配置版本、订单快照）。"""
        row = self.conn.execute(
            "SELECT * FROM certificate WHERE cert_id = ?", (cert_id,)
        ).fetchone()
        if row is None:
            raise InspectionError(f"证书 {cert_id!r} 不存在", reason="no-such-certificate")
        return {
            "cert_id": row["cert_id"],
            "submission_id": row["submission_id"],
            "order_id": row["order_id"],
            "created_at": row["created_at"],
            "digest": row["digest"],
            "payload": json.loads(row["payload_json"]),
        }

    def verify_certificate(self, cert_id: str) -> str:
        row = self.conn.execute(
            "SELECT payload_json, digest FROM certificate WHERE cert_id = ?", (cert_id,)
        ).fetchone()
        if row is None:
            raise InspectionError(f"证书 {cert_id!r} 不存在", reason="no-such-certificate")
        actual = hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest()
        return "ok" if actual == row["digest"] else "digest-mismatch"

    def get_submission(self, submission_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM submission WHERE submission_id = ?", (submission_id,)
        ).fetchone()
        if row is None:
            raise InspectionError(f"送审 {submission_id!r} 不存在", reason="no-such-submission")
        return self._submission(row)

    def list_submissions(self, order_id: str | None = None) -> list[dict]:
        if order_id is None:
            rows = self.conn.execute("SELECT * FROM submission ORDER BY created_at").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM submission WHERE order_id = ? ORDER BY created_at", (order_id,)
            ).fetchall()
        return [self._submission(r) for r in rows]

    def list_certificates(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT cert_id, submission_id, order_id, created_at, digest FROM certificate"
            " ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _submission(row: sqlite3.Row) -> dict:
        return {
            "submission_id": row["submission_id"],
            "order_id": row["order_id"],
            "product": row["product"],
            "version": row["version"],
            "doc_name": row["doc_name"],
            "task_id": row["task_id"],
            "source_csv": row["source_csv"],
            "created_at": row["created_at"],
            "result": row["result"],
            "report": json.loads(row["report_json"]),
        }


# --------------------------------------------------------------------------- #
# 签发机构：把目录、接入仓、证书仓串成一次送审
# --------------------------------------------------------------------------- #
class CertificateAuthority:
    """一次送审 = 解析采购单 -> 复用接入仓读 CSV -> 解析候补 -> 评估 -> 落库。"""

    def __init__(self, catalog: Catalog, tasks: TaskRepository, certs: CertificateRepository):
        self.catalog = catalog
        self.tasks = tasks
        self.certs = certs

    def explain_fallbacks(
        self, product: str, doc_name: str, version: int | None = None
    ) -> dict:
        """目录级解释候补关系（不需要任何测量数据）。"""
        doc = self.catalog.load_document(product, doc_name, version)
        return FallbackGraph.from_document(doc).describe()

    def submit(
        self,
        submission_id: str,
        product: str,
        doc_name: str,
        order_yaml: str,
        csv_path: str | Path,
        version: int | None = None,
    ) -> dict:
        """执行一次送审，返回可复核报告（同时落库）。

        * 采购单/抽检配置结构违规 -> :class:`InspectionError`，不留任何记录；
        * CSV 接入违规 -> :class:`confcat.errors.TaskRejected`（沿用原合同），不留记录；
        * 评估不通过 -> 送审记录落库（result=rejected），可凭后到测量重新送审。
        """
        if self.certs.has_submission(submission_id):
            raise InspectionError(f"送审 {submission_id!r} 已存在", reason="duplicate-submission")
        order = parse_order(order_yaml)
        version = self.catalog._resolve_version(product, version)
        doc = self.catalog.load_document(product, doc_name, version)
        graph = FallbackGraph.from_document(doc)

        # 复用既有接入仓读取检测台 CSV（同一产品文档的 ingest: 段）
        task_id = f"insp-{submission_id}"
        self.tasks.create_task(task_id, product, doc_name, csv_path, version)
        rows = self.tasks.get_rows(task_id)
        token = self.tasks.get_params(task_id)["missing_value"]["token"]

        resolver = Resolver(
            graph,
            measurements=rows[0] if rows else {},
            missing_token=token,
            meas_ns=task_id,
            sample_row=0,
        )
        resolver.resolve_all()
        report = evaluate(order, graph, resolver)

        created_at = datetime.now(timezone.utc).isoformat()
        report.update(
            {
                "submission_id": submission_id,
                "order_id": order.order_id,
                "product": product,
                "version": version,
                "doc_name": doc_name,
                "task_id": task_id,
                "source_csv": str(csv_path),
                "created_at": created_at,
            }
        )
        self.certs.record_submission(
            submission_id,
            order.order_id,
            product,
            version,
            doc_name,
            task_id,
            str(csv_path),
            report["result"],
            report,
            created_at,
        )
        if report["result"] == "issued":
            cert_id = f"cert-{submission_id}"
            payload = {
                "cert_id": cert_id,
                "order": order.as_dict(),
                "product": product,
                "version": version,
                "doc_name": doc_name,
                "task_id": task_id,
                "assignments": report["assignment"],
                "evidence": self._evidence_table(report["assignment"]),
                "created_at": created_at,
            }
            digest = self.certs.issue_certificate(
                cert_id, submission_id, order.order_id, payload, created_at
            )
            report["cert_id"] = cert_id
            report["cert_digest"] = digest
        return report

    @staticmethod
    def _evidence_table(assignments: list[dict]) -> dict:
        """按依据 id 去重的依据台账：两格共用一份依据 => 台账只有一条。"""
        table: dict[str, dict] = {}
        for a in assignments:
            ev = a["evidence"]
            if ev in table:
                continue
            last_hop = a["chain"][-1] if a["chain"] else {}
            if a["source_kind"] == "measurement":
                table[ev] = {
                    "kind": "measurement",
                    "task": last_hop.get("task"),
                    "column": last_hop.get("column"),
                    "row": last_hop.get("row"),
                    "raw": last_hop.get("raw"),
                }
            else:
                table[ev] = {
                    "kind": "standard",
                    "id": last_hop.get("id"),
                    "value": last_hop.get("value"),
                    "config_line": last_hop.get("config_line"),
                    "alias_chain": last_hop.get("alias_chain", []),
                }
        return table
