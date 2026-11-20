"""外观抽检的候补关系图与取值解析引擎。

配置合同（写在既有目录产品的同一文档内，与 ``ingest:`` 段并存）
================================================================
* ``standards``：标准样声明区。声明为映射 ``{id: 可选 str, value: int}``；
  **声明的身份 = 锚点（别名链）或内联位置**——不同路径用别名 ``*x`` 引用同一
  声明时，物化树的 ``Provenance.alias_chain`` 指向同一锚点，只算一份依据；
  两处各自内联的同名同值声明是两份依据（文本独立才算独立）；
* ``stations.<台>.columns.<指标> = <CSV 目标列>``：本机实测列；
* ``stations.<台>.fallback.<指标> = [规则...]``：本机列缺测时的**有序候补**：
    - ``cell: {station: 台, metric: 指标}`` 引用另一台的原测格（递归解析）；
    - ``standard: <声明>`` 使用配置中的标准样值（通常经别名引用声明区）。

解析合同
========
* 每格 ``(台, 指标)`` 先读本机实测列；缺测（值等于 missing token 或列未接入）
  才按序走候补，**第一个取到有效值的候补即为本格来源**；
* 解析输出本格实际取到的原始测量或标准样来源，保留完整引用链；链中台/指标
  以类型化键（``{"kt": "s"|"i"}``）落库，``1`` 与 ``"1"`` 仍是两个键；
* 依据身份：实测 = ``meas:<任务>:<台>:<指标>``（格身份，他台引用本格即共用）；
  标准样 = 声明锚点身份。**两格共用一次实测或同一标准样声明 => 依据 id 相同**；
* 未走到的候补只登记 ``skipped``，不占用依据；
* 循环候补按该支路失败处理并记录环；全部支路失败 => 该格无有效结论，
  经 :meth:`Resolver.resolve_all` / :meth:`Resolver.unresolved` 可查询。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .errors import InspectionError
from .model import (
    ConfigKey,
    MatMapping,
    MatNode,
    MatScalar,
    MatSequence,
    MaterializedDocument,
    Provenance,
)

# 与 yaml_parser 同一整数合同：0 或负号可选的非零首位；其余（007、1.5…）非数值
_INT_RE = re.compile(r"^(0|-?[1-9][0-9]*)$")


def parse_measurement(raw: str) -> int | None:
    """把 CSV 归一化后的原始字符串按目录整数合同解析；非数值返回 None。"""
    return int(raw) if _INT_RE.match(raw) else None


def key_tag(key: ConfigKey) -> str:
    """类型化键 -> 稳定字符串（依据 id 用）：``s:alpha`` / ``i:1``。"""
    if isinstance(key, bool) or not isinstance(key, (str, int)):
        raise InspectionError(f"键类型非法: {key!r}", reason="bad-key-type")
    return f"i:{key}" if isinstance(key, int) else f"s:{key}"


def key_token(key: ConfigKey) -> dict:
    """类型化键 -> JSON 结构（引用链用），重开后类型不丢失。"""
    if isinstance(key, bool) or not isinstance(key, (str, int)):
        raise InspectionError(f"键类型非法: {key!r}", reason="bad-key-type")
    return {"kt": "i" if isinstance(key, int) else "s", "k": key}


def cell_label(cell: tuple) -> str:
    return f"{key_tag(cell[0])}/{key_tag(cell[1])}"


def flatten_reasons(res: "Resolution") -> list[str]:
    """把一格的失败尝试（含引用链内层带出的原因）摊平成可检索的字符串列表。"""
    out: list[str] = []
    for a in res.attempts:
        reason = a.get("reason")
        if reason:
            out.append(reason)
        out.extend(a.get("detail", []))
    return out


def _sort_key(key: ConfigKey) -> tuple:
    return (type(key).__name__, str(key))


# --------------------------------------------------------------------------- #
# 候补关系图（纯配置层，不需要测量数据即可“表达和解释候补关系”）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CellRule:
    """一条有序候补规则。"""

    index: int  # 在该格候补序列中的序号（0 基）
    kind: str  # "cell" | "standard"
    target: tuple | None  # kind=cell：被引用的 (台, 指标)
    standard_identity: str | None  # kind=standard：声明锚点身份（依据 id）
    standard_id: str | None  # 声明的人类可读 id（可选键 id）
    standard_value: int | None  # 标准样值（int）
    prov: Provenance | None  # 规则值节点的配置来源（含别名链）
    decl_chain: tuple = ()  # kind=standard：声明子键携带的完整别名链（展示用）

    def describe(self) -> dict:
        d: dict[str, Any] = {"index": self.index, "kind": self.kind}
        if self.kind == "cell":
            d["target"] = {"station": key_token(self.target[0]), "metric": key_token(self.target[1])}
        else:
            d["standard"] = {
                "identity": self.standard_identity,
                "id": self.standard_id,
                "value": self.standard_value,
            }
        if self.prov is not None:
            d["config_line"] = self.prov.value_mark.line
        if self.kind == "standard":
            d["alias_chain"] = list(self.decl_chain)
        return d


@dataclass
class StationDef:
    key: ConfigKey
    label: str | None
    columns: dict  # 指标 -> CSV 目标列
    fallback: dict  # 指标 -> [CellRule, ...]（有序）


class FallbackGraph:
    """从物化配置文档提取的候补关系图。"""

    def __init__(self, stations: dict, doc_index: int = 0):
        self.stations: dict[ConfigKey, StationDef] = stations
        self.doc_index = doc_index
        universe: set[tuple] = set()
        for s_key, st in stations.items():
            for m in st.columns:
                universe.add((s_key, m))
            for m in st.fallback:
                universe.add((s_key, m))
        self.universe: frozenset[tuple] = frozenset(universe)

    # ------------------------------------------------------------------ #
    @classmethod
    def from_document(cls, doc: MaterializedDocument) -> "FallbackGraph":
        root = doc.root
        if not isinstance(root, MatMapping):
            raise InspectionError("抽检配置根必须是映射", reason="bad-root")
        stations_node = root.entries.get("stations")
        if stations_node is None:
            raise InspectionError("缺少 stations 段", path=("stations",), reason="missing-path")
        if not isinstance(stations_node, MatMapping):
            raise InspectionError("stations 必须是映射", path=("stations",), reason="bad-type")
        stations: dict[ConfigKey, StationDef] = {}
        for s_key, s_node in stations_node.entries.items():
            stations[s_key] = cls._station(s_key, s_node)
        return cls(stations, doc_index=doc.index)

    @classmethod
    def _station(cls, s_key: ConfigKey, node: MatNode) -> StationDef:
        path = ("stations", s_key)
        if not isinstance(node, MatMapping):
            raise InspectionError("检测台定义必须是映射", path=path, reason="bad-type")
        label = None
        label_node = node.entries.get("label")
        if label_node is not None:
            if not isinstance(label_node, MatScalar) or not isinstance(label_node.value, str):
                raise InspectionError("label 必须是字符串", path=path + ("label",), reason="bad-type")
            label = label_node.value
        columns: dict = {}
        col_node = node.entries.get("columns")
        if col_node is not None:
            if not isinstance(col_node, MatMapping):
                raise InspectionError("columns 必须是映射", path=path + ("columns",), reason="bad-type")
            for m_key, m_node in col_node.entries.items():
                if not isinstance(m_node, MatScalar) or not isinstance(m_node.value, str):
                    raise InspectionError(
                        "columns.<指标> 必须是字符串 CSV 目标列名",
                        path=path + ("columns", m_key),
                        reason="bad-type",
                    )
                columns[m_key] = m_node.value
        fallback: dict = {}
        fb_node = node.entries.get("fallback")
        if fb_node is not None:
            if not isinstance(fb_node, MatMapping):
                raise InspectionError("fallback 必须是映射", path=path + ("fallback",), reason="bad-type")
            for m_key, rules_node in fb_node.entries.items():
                fallback[m_key] = cls._rules(rules_node, path + ("fallback", m_key))
        return StationDef(key=s_key, label=label, columns=columns, fallback=fallback)

    @classmethod
    def _rules(cls, node: MatNode, path: tuple) -> list[CellRule]:
        if not isinstance(node, MatSequence):
            raise InspectionError("候补规则必须是序列", path=path, reason="bad-type")
        rules: list[CellRule] = []
        for idx, item in enumerate(node.items):
            rules.append(cls._rule(item, idx, path + (idx,)))
        return rules

    @classmethod
    def _rule(cls, node: MatNode, index: int, path: tuple) -> CellRule:
        if not isinstance(node, MatMapping):
            raise InspectionError("候补规则必须是映射", path=path, reason="bad-type")
        kinds = [k for k in ("cell", "standard") if k in node.entries]
        extra = [k for k in node.entries if k not in ("cell", "standard")]
        if len(kinds) != 1 or extra:
            raise InspectionError(
                "候补规则必须且只能含 cell / standard 之一",
                path=path,
                reason="bad-rule",
            )
        prov = node.key_prov.get(kinds[0])
        if kinds[0] == "cell":
            return cls._cell_rule(node.entries["cell"], index, path + ("cell",), prov)
        return cls._standard_rule(node.entries["standard"], index, path + ("standard",), prov)

    @classmethod
    def _cell_rule(cls, node: MatNode, index: int, path: tuple, prov) -> CellRule:
        if not isinstance(node, MatMapping):
            raise InspectionError("cell 候补必须是映射 {station, metric}", path=path, reason="bad-type")
        target: list[ConfigKey] = []
        for need in ("station", "metric"):
            v = node.entries.get(need)
            if v is None or not isinstance(v, MatScalar) or isinstance(v.value, bool) or not isinstance(
                v.value, (str, int)
            ):
                raise InspectionError(
                    f"cell 候补缺少类型化键 {need}（str/int 标量）",
                    path=path + (need,),
                    reason="bad-type",
                )
            target.append(v.value)
        return CellRule(
            index=index,
            kind="cell",
            target=(target[0], target[1]),
            standard_identity=None,
            standard_id=None,
            standard_value=None,
            prov=prov,
        )

    @classmethod
    def _standard_rule(cls, node: MatNode, index: int, path: tuple, prov) -> CellRule:
        if not isinstance(node, MatMapping):
            raise InspectionError("standard 候补必须是标准样声明映射", path=path, reason="bad-type")
        value_node = node.entries.get("value")
        if (
            value_node is None
            or not isinstance(value_node, MatScalar)
            or isinstance(value_node.value, bool)
            or not isinstance(value_node.value, int)
        ):
            raise InspectionError(
                "标准样声明缺少 int 类型的 value", path=path + ("value",), reason="bad-type"
            )
        std_id = None
        id_node = node.entries.get("id")
        if id_node is not None:
            if not isinstance(id_node, MatScalar) or not isinstance(id_node.value, str):
                raise InspectionError("标准样 id 必须是字符串", path=path + ("id",), reason="bad-type")
            std_id = id_node.value
        # 声明身份：声明映射子键的来源携带从引用点到锚点的完整别名链
        # （显式键的键级 prov 不含其值自身的别名跳，子键 prov 才有——与
        # test_scale 中 uses/uN/k 的既有合同一致）。末跳锚点即声明本身：
        # 不同路径经任意中间别名到达同一锚点 => 同一身份，只算一份依据；
        # 无别名 => 内联声明，以其自身位置为身份（文本独立才算独立）。
        decl_prov = value_node.prov or node.prov or prov
        chain = decl_prov.alias_chain if decl_prov else ()
        if chain:
            last = chain[-1]
            identity = (
                f"std:{last.alias_name}@{last.anchor_mark.doc_index}:"
                f"{last.anchor_mark.line}:{last.anchor_mark.column}"
            )
        else:
            m = (node.prov or prov).value_mark if (node.prov or prov) else None
            identity = f"std:inline@{m.doc_index}:{m.line}:{m.column}" if m else "std:inline@?"
        return CellRule(
            index=index,
            kind="standard",
            target=None,
            standard_identity=identity,
            standard_id=std_id,
            standard_value=value_node.value,
            prov=prov,
            decl_chain=tuple(e.alias_name for e in chain),
        )

    # ------------------------------------------------------------------ #
    def cells(self) -> list[tuple]:
        """全图所有格（确定性顺序；混合类型键不直接比较）。"""
        return sorted(self.universe, key=lambda c: (_sort_key(c[0]), _sort_key(c[1])))

    def has_cell(self, cell: tuple) -> bool:
        return cell in self.universe

    def column_of(self, cell: tuple) -> str | None:
        st = self.stations.get(cell[0])
        return st.columns.get(cell[1]) if st else None

    def rules_of(self, cell: tuple) -> list[CellRule]:
        st = self.stations.get(cell[0])
        return st.fallback.get(cell[1], []) if st else []

    def describe(self) -> dict:
        """目录级解释：每格的本机列与有序候补（含配置行号、别名链、标准样身份）。"""
        out = {"doc_index": self.doc_index, "stations": [], "cells": []}
        for s_key in sorted(self.stations, key=_sort_key):
            st = self.stations[s_key]
            out["stations"].append({"key": key_token(s_key), "label": st.label})
        for cell in self.cells():
            out["cells"].append(
                {
                    "cell": {"station": key_token(cell[0]), "metric": key_token(cell[1])},
                    "local_column": self.column_of(cell),
                    "ordered_fallbacks": [r.describe() for r in self.rules_of(cell)],
                }
            )
        return out


# --------------------------------------------------------------------------- #
# 解析：本机实测 -> 有序候补（他台原测格 / 标准样）
# --------------------------------------------------------------------------- #
@dataclass
class Resolution:
    """一格的解析结果。``chain`` 只含实际走到的来源；``skipped`` 不占用依据。"""

    cell: tuple
    status: str  # "resolved" | "unresolved" | "cycle"（cycle 仅解析内部一站）
    value: int | None = None  # 数值化后的结论（非数值测量为 None）
    raw: str | None = None  # 原始测量字符串或标准样值文本
    evidence: str | None = None  # 最终依据 id（实测格身份 / 标准样声明身份）
    source_kind: str | None = None  # "measurement" | "standard"
    chain: tuple = ()  # 引用链（本机列 / cell 跳转 / 标准样）
    attempts: tuple = ()  # 已尝试但失败的候补（含本机列缺测），带原因
    skipped: tuple = ()  # 排在使用来源之后、从未评估的候补
    cycle_path: tuple = ()  # status=cycle 时的环路径

    def to_dict(self) -> dict:
        return {
            "cell": {"station": key_token(self.cell[0]), "metric": key_token(self.cell[1])},
            "status": self.status,
            "value": self.value,
            "raw": self.raw,
            "evidence": self.evidence,
            "source_kind": self.source_kind,
            "chain": list(self.chain),
            "attempts": list(self.attempts),
            "skipped": list(self.skipped),
        }


class Resolver:
    """按图解析各格实际来源。

    ``measurements``：CSV 目标列 -> 归一化后的原始字符串（接入任务首行快照）；
    ``missing_token``：等于该 token 视为缺测；``meas_ns``：实测依据命名空间
    （取接入任务 id，重开后仍能定位是哪次接入的哪一格）。
    """

    def __init__(
        self,
        graph: FallbackGraph,
        measurements: dict[str, str],
        missing_token: str = "",
        meas_ns: str = "meas",
        sample_row: int = 0,
    ):
        self.graph = graph
        self.measurements = dict(measurements)
        self.missing_token = missing_token
        self.meas_ns = meas_ns
        self.sample_row = sample_row
        self._memo: dict[tuple, Resolution] = {}
        self._last_all: dict[tuple, Resolution] = {}

    # ------------------------------------------------------------------ #
    def resolve(self, cell: tuple) -> Resolution:
        res, _clean = self._resolve(cell, ())
        return res

    def resolve_all(self) -> dict[tuple, Resolution]:
        self._last_all = {cell: self.resolve(cell) for cell in self.graph.cells()}
        return dict(self._last_all)

    def unresolved(self) -> dict[tuple, Resolution]:
        """全图中无有效结论的格（循环或全缺）；需先 resolve_all。"""
        return {c: r for c, r in self._last_all.items() if r.status != "resolved"}

    # ------------------------------------------------------------------ #
    def _resolve(self, cell: tuple, stack: tuple) -> tuple[Resolution, bool]:
        """返回 (结果, 是否可安全记忆)。途经循环支路的结果与解析栈相关，不记忆。"""
        if cell in self._memo:
            return self._memo[cell], True
        if cell in stack:
            return Resolution(cell=cell, status="cycle", cycle_path=stack + (cell,)), False

        attempts: list[dict] = []
        clean = True
        rules = self.graph.rules_of(cell)

        # 1) 本机实测列优先
        col = self.graph.column_of(cell)
        if col is not None:
            raw = self.measurements.get(col)
            if raw is not None and raw != self.missing_token:
                res = Resolution(
                    cell=cell,
                    status="resolved",
                    value=parse_measurement(raw),
                    raw=raw,
                    evidence=f"meas:{self.meas_ns}:{key_tag(cell[0])}:{key_tag(cell[1])}",
                    source_kind="measurement",
                    chain=(
                        {
                            "hop": "local",
                            "column": col,
                            "task": self.meas_ns,
                            "row": self.sample_row,
                            "raw": raw,
                        },
                    ),
                    attempts=(),
                    skipped=self._skipped(rules, -1),
                )
                self._memo[cell] = res
                return res, True
            reason = "missing-token" if raw is not None else "column-not-ingested"
            attempts.append({"rule": "local", "column": col, "reason": reason})

        # 2) 有序候补：第一个取到有效值的即为本格来源
        for idx, rule in enumerate(rules):
            if rule.kind == "standard":
                res = Resolution(
                    cell=cell,
                    status="resolved",
                    value=rule.standard_value,
                    raw=str(rule.standard_value),
                    evidence=rule.standard_identity,
                    source_kind="standard",
                    chain=(self._standard_hop(rule),),
                    attempts=tuple(attempts),
                    skipped=self._skipped(rules, idx),
                )
                if clean:
                    self._memo[cell] = res
                return res, clean
            target = rule.target
            if not self.graph.has_cell(target):
                attempts.append(
                    {
                        "rule": idx,
                        "kind": "cell",
                        "target": cell_label(target),
                        "reason": "no-such-cell",
                    }
                )
                continue
            sub, sub_clean = self._resolve(target, stack + (cell,))
            clean = clean and sub_clean
            if sub.status == "resolved":
                hop = {
                    "hop": "cell",
                    "station": key_token(target[0]),
                    "metric": key_token(target[1]),
                }
                res = Resolution(
                    cell=cell,
                    status="resolved",
                    value=sub.value,
                    raw=sub.raw,
                    evidence=sub.evidence,  # 引用他台原测格 => 共用同一份依据
                    source_kind=sub.source_kind,
                    chain=(hop,) + sub.chain,
                    attempts=tuple(attempts),
                    skipped=self._skipped(rules, idx),
                )
                if clean:
                    self._memo[cell] = res
                return res, clean
            if sub.status == "cycle":
                reason = "cycle: " + " -> ".join(cell_label(c) for c in sub.cycle_path)
                detail: list[str] = []
            else:
                reason = f"target-unresolved: {cell_label(target)}"
                detail = flatten_reasons(sub)  # 内层失败原因（含循环）递归带出
            attempts.append(
                {
                    "rule": idx,
                    "kind": "cell",
                    "target": cell_label(target),
                    "reason": reason,
                    "detail": detail,
                }
            )

        res = Resolution(cell=cell, status="unresolved", attempts=tuple(attempts))
        if clean:
            self._memo[cell] = res
        return res, clean

    # ------------------------------------------------------------------ #
    @staticmethod
    def _skipped(rules: list[CellRule], taken_idx: int) -> tuple:
        """排在实际来源之后的候补：从未评估，不占用依据。"""
        return tuple(
            {"rule": r.index, "kind": r.kind, "reason": "not-reached"}
            for r in rules
            if r.index > taken_idx
        )

    @staticmethod
    def _standard_hop(rule: CellRule) -> dict:
        hop = {
            "hop": "standard",
            "identity": rule.standard_identity,
            "id": rule.standard_id,
            "value": rule.standard_value,
            "alias_chain": list(rule.decl_chain),
        }
        if rule.prov is not None:
            hop["config_line"] = rule.prov.value_mark.line
        return hop
