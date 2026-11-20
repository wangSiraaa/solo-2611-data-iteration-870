"""命令行入口：目录可独立用于人工检查。

示例
====
::

    python -m confcat.cli publish ./data/catalog.db orders-product samples/orders_config.yaml
    python -m confcat.cli products ./data/catalog.db
    python -m confcat.cli show ./data/catalog.db orders-product doc-0
    python -m confcat.cli query ./data/catalog.db orders-product doc-0 ingest/column_mapping
    python -m confcat.cli verify ./data/catalog.db orders-product
    python -m confcat.cli tasks ./data/tasks.db

外观抽检联合证书：

    python -m confcat.cli fallbacks ./data/catalog.db appearance inspect
    python -m confcat.cli submit ./data/catalog.db ./data/tasks.db ./data/certs.db \
        sub-1 appearance inspect samples/order_po001.yaml data/stations_v1.csv
    python -m confcat.cli submissions ./data/certs.db --order PO-2026-001
    python -m confcat.cli certificate ./data/certs.db cert-sub-1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .catalog import Catalog
from .codec import path_str
from .ingest import TaskRepository


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="confcat", description="受限 YAML 配置目录与消费仓")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pub = sub.add_parser("publish", help="发布 YAML 为新产品版本")
    p_pub.add_argument("db")
    p_pub.add_argument("product")
    p_pub.add_argument("yaml_path")
    p_pub.add_argument("--docs", nargs="*", help="多文档命名，顺序对应文档序号")

    p_prod = sub.add_parser("products", help="列出产品与版本")
    p_prod.add_argument("db")

    p_docs = sub.add_parser("docs", help="列出某产品文档")
    p_docs.add_argument("db")
    p_docs.add_argument("product")
    p_docs.add_argument("--version", type=int)

    p_show = sub.add_parser("show", help="导出文档有效配置（普通 JSON 视图）")
    p_show.add_argument("db")
    p_show.add_argument("product")
    p_show.add_argument("doc")
    p_show.add_argument("--version", type=int)

    p_q = sub.add_parser("query", help="查询路径有效值与来源证据")
    p_q.add_argument("db")
    p_q.add_argument("product")
    p_q.add_argument("doc")
    p_q.add_argument("path")
    p_q.add_argument("--version", type=int)

    p_v = sub.add_parser("verify", help="重算哈希并回查配置链完整性")
    p_v.add_argument("db")
    p_v.add_argument("product")
    p_v.add_argument("--version", type=int)

    p_run = sub.add_parser("run-task", help="创建接入任务（消费仓）")
    p_run.add_argument("catalog_db")
    p_run.add_argument("task_db")
    p_run.add_argument("task_id")
    p_run.add_argument("product")
    p_run.add_argument("doc")
    p_run.add_argument("csv_path")
    p_run.add_argument("--version", type=int)

    p_tasks = sub.add_parser("tasks", help="列出消费仓任务")
    p_tasks.add_argument("task_db")
    p_tasks.add_argument("catalog_db")

    p_rows = sub.add_parser("rows", help="逐行查看任务结果")
    p_rows.add_argument("task_db")
    p_rows.add_argument("catalog_db")
    p_rows.add_argument("task_id")

    p_fb = sub.add_parser("fallbacks", help="解释抽检配置的候补关系（目录级，无需测量）")
    p_fb.add_argument("db")
    p_fb.add_argument("product")
    p_fb.add_argument("doc")
    p_fb.add_argument("--version", type=int)

    p_sub = sub.add_parser("submit", help="外观抽检送审（评估并尝试签发联合证书）")
    p_sub.add_argument("catalog_db")
    p_sub.add_argument("task_db")
    p_sub.add_argument("cert_db")
    p_sub.add_argument("submission_id")
    p_sub.add_argument("product")
    p_sub.add_argument("doc")
    p_sub.add_argument("order_yaml")
    p_sub.add_argument("csv_path")
    p_sub.add_argument("--version", type=int)

    p_subs = sub.add_parser("submissions", help="列出送审记录（可按采购单过滤）")
    p_subs.add_argument("cert_db")
    p_subs.add_argument("--order")

    p_cert = sub.add_parser("certificate", help="离线重开已发证书并校验摘要")
    p_cert.add_argument("cert_db")
    p_cert.add_argument("cert_id")

    args = parser.parse_args(argv)

    if args.cmd == "publish":
        with Catalog(args.db) as cat:
            info = cat.publish_file(args.product, Path(args.yaml_path), doc_names=args.docs)
        _print(
            {
                "product": info.product,
                "version": info.version,
                "docs": list(info.docs),
                "digest": info.digest,
            }
        )
        return 0

    # 证书仓专有命令：离线重开，不依赖目录与源 CSV
    if args.cmd in ("submissions", "certificate"):
        from .certificate import CertificateRepository

        with CertificateRepository(args.cert_db) as certs:
            if args.cmd == "submissions":
                _print(certs.list_submissions(order_id=args.order))
            else:
                _print(
                    {
                        "verify": certs.verify_certificate(args.cert_id),
                        **certs.get_certificate(args.cert_id),
                    }
                )
        return 0

    is_task_cmd = args.cmd in ("run-task", "tasks", "rows")
    catalog_db = args.catalog_db if is_task_cmd or args.cmd == "submit" else args.db
    with Catalog(catalog_db) as cat:
        if args.cmd == "products":
            _print(
                [
                    {
                        "product": p.product,
                        "version": p.version,
                        "docs": list(p.docs),
                        "source_path": p.source_path,
                        "digest": p.digest,
                        "created_at": p.created_at,
                    }
                    for p in cat.list_products()
                ]
            )
        elif args.cmd == "docs":
            _print(cat.list_docs(args.product, args.version))
        elif args.cmd == "show":
            _print(cat.export_plain(args.product, args.doc, args.version))
        elif args.cmd == "query":
            r = cat.query(args.product, args.doc, args.path, args.version)
            _print(
                {
                    "product": r.product,
                    "version": r.version,
                    "doc": r.doc_name,
                    "path": path_str(r.path),
                    "value": r.value,
                    "provenance": r.provenance.as_dict() if r.provenance else None,
                }
            )
        elif args.cmd == "verify":
            _print(cat.verify(args.product, args.version))
        elif args.cmd == "fallbacks":
            from .fallback import FallbackGraph

            doc = cat.load_document(args.product, args.doc, args.version)
            _print(FallbackGraph.from_document(doc).describe())
        elif args.cmd == "submit":
            from .certificate import CertificateAuthority, CertificateRepository

            with TaskRepository(args.task_db, cat) as repo, CertificateRepository(
                args.cert_db
            ) as certs:
                auth = CertificateAuthority(cat, repo, certs)
                report = auth.submit(
                    args.submission_id,
                    args.product,
                    args.doc,
                    Path(args.order_yaml).read_text(encoding="utf-8"),
                    args.csv_path,
                    args.version,
                )
            _print(report)
        elif is_task_cmd:
            with TaskRepository(args.task_db, cat) as repo:
                if args.cmd == "run-task":
                    summary = repo.create_task(
                        args.task_id, args.product, args.doc, args.csv_path, args.version
                    )
                    _print(summary.__dict__ | {"partitions": summary.partitions})
                elif args.cmd == "tasks":
                    _print([s.__dict__ for s in repo.list_tasks()])
                else:
                    _print(
                        {
                            "summary": repo.get_task(args.task_id).__dict__,
                            "params": repo.get_params(args.task_id),
                            "lineage": repo.get_lineage(args.task_id),
                            "rows": repo.get_rows(args.task_id),
                        }
                    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
