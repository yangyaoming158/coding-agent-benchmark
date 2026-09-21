"""从已有实验生成 HTML、Markdown、JSON 报告。

示例：

    python -m cli.report generate --run 125 --run 126 --run 127

命令只读取既有实验数据并写报告制品，不会入队、启动 Worker 或调用模型。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.infrastructure.db import create_db_engine, create_session_factory, session_scope
from app.report import ReportInputError, generate_report
from app.storage import create_artifact_store


def cmd_generate(args: argparse.Namespace) -> int:
    """生成报告并打印三份制品的位置。"""
    factory = create_session_factory(create_db_engine())
    store = create_artifact_store()
    try:
        with session_scope(factory) as session:
            result = generate_report(
                session,
                store,
                args.run,
                title=args.title,
                base_url=args.base_url,
                top_n=args.top_n,
                host_metrics_csv=args.host_metrics_csv,
            )
    except ReportInputError as exc:
        print(f"无法生成报告：{exc}")
        return 2

    print(
        f"报告已生成：{result.data.scope}，数据集 "
        f"{', '.join(dataset.slug + '@' + dataset.version for dataset in result.data.datasets)}，"
        f"运行 {', '.join('#' + str(i) for i in result.data.run_ids)}"
    )
    for report_format, ref in result.artifacts.items():
        print(
            f"  {report_format.value:<8} record=#{result.record_ids[report_format]} "
            f"sha256={ref.sha256[:12]}… {ref.uri}"
        )
    if result.data.warnings:
        print("\n数据完整性说明：")
        for warning in result.data.warnings:
            print(f"  - {warning}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构造报告命令行参数。"""
    parser = argparse.ArgumentParser(prog="cli.report", description="评测报告生成器（E10-T3）")
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate", help="从已有实验生成并登记三种报告制品")
    generate.add_argument(
        "--run", type=int, action="append", required=True, help="实验 ID；对比报告可重复给多个"
    )
    generate.add_argument("--title", default="AI Coding Agent 评测对比报告")
    generate.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Top 失败案例中补丁、日志和轨迹链接的 API 根地址",
    )
    generate.add_argument("--top-n", type=int, default=10, choices=range(1, 101))
    generate.add_argument(
        "--host-metrics-csv",
        type=Path,
        help="可选宿主采样 CSV；读取 cpu_pct 和 used_pct，不提供就明确标不可用",
    )
    generate.set_defaults(func=cmd_generate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "cmd_generate", "main"]
