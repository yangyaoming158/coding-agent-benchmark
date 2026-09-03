"""题目的导入 / 导出 / Schema 命令（E1-T1）。

    python -m cli.task import datasets/golden/*.json      # 校验，坏题给出可读原因
    python -m cli.task export task.json --out norm.json   # 规范化并回填 content_hash
    python -m cli.task schema --out ../schemas/task.schema.json
    python -m cli.task schema --check                     # CI 用：文件和模型对不上就非零退出

**这一层只处理文件，不写数据库。** 入库要先有 `repositories` 和 `environment_specs`
的行（题目表对它们有外键），那是 E1-T2 / E8 的事。分开的好处是：现在就能用真实的
题目 JSON 把 Schema 和校验规则跑通，不用等仓库选型定档。

支持 `.json`（单道题）和 `.jsonl`（一行一道题，数据集导出格式）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path

from pydantic import ValidationError

from app.benchmark.schema import TaskDefinition

#: JSON Schema 的落盘位置（仓库根的 schemas/）。它是对外的数据契约，
#: 和 docs/evaluation-protocol.md 一样属于"别人要照着做"的东西，所以放仓库根。
DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "task.schema.json"


def render_schema() -> str:
    """把 Pydantic 模型导成 JSON Schema 文本。

    `sort_keys=True` 是必需的：不排序的话，同一个模型两次导出的键序可能不同，
    每次跑都产生一份"有改动"的文件，diff 里全是噪声，漂移检查也就失去意义了。
    """
    schema = TaskDefinition.model_json_schema()
    return json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def iter_task_payloads(path: Path) -> Iterator[tuple[int, dict[str, object]]]:
    """从 .json 或 .jsonl 里读出一道道题，附带行号（.json 固定为 1）。

    行号是给报错用的：一个 100 行的 jsonl 里有一道坏题，没有行号就只能一行行数。
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.strip():
                yield lineno, json.loads(line)
    else:
        yield 1, json.loads(text)


def format_errors(exc: ValidationError) -> list[str]:
    """把 Pydantic 的报错整理成人能读的几行。

    默认的 `str(exc)` 会带上一大段模型名和文档链接，对着一屏报错找"到底哪不对"
    很费劲。这里只留"哪个字段 + 什么问题"。
    """
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(整个对象)"
        lines.append(f"    {location}: {error['msg']}")
    return lines


def cmd_import(args: argparse.Namespace) -> int:
    """校验一批题目文件。全部通过返回 0，有坏题返回 1。"""
    total = 0
    bad = 0
    for raw_path in args.paths:
        path = Path(raw_path)
        for lineno, payload in iter_task_payloads(path):
            total += 1
            label = f"{path}" + (f":{lineno}" if path.suffix == ".jsonl" else "")
            try:
                task = TaskDefinition.model_validate(payload)
            except ValidationError as exc:
                bad += 1
                print(f"✗ {label}", file=sys.stderr)
                for line in format_errors(exc):
                    print(line, file=sys.stderr)
                continue
            flags = task.review_flags()
            mark = "!" if flags else "✓"
            print(f"{mark} {label}  {task.task_id}  {task.content_hash}")
            for flag in flags:
                print(f"    需人工复核：{flag}")

    print(f"\n共 {total} 道，通过 {total - bad}，拒收 {bad}")
    return 1 if bad else 0


def cmd_export(args: argparse.Namespace) -> int:
    """读进来、规范化、写出去。

    规范化会做三件事：集合类字段排序去重、`test_patch_paths` 按 test_patch 重算、
    回填 `content_hash`。所以这个命令也是"给手写的题目补齐哈希"的正规做法。
    """
    payload = json.loads(Path(args.path).read_text(encoding="utf-8"))
    task = TaskDefinition.model_validate(payload)
    text = json.dumps(task.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"已写出 {args.out}（content_hash={task.content_hash}）")
    else:
        sys.stdout.write(text)
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    """导出 JSON Schema；`--check` 只比对不写。"""
    target = Path(args.out) if args.out else DEFAULT_SCHEMA_PATH
    rendered = render_schema()

    if args.check:
        if not target.exists():
            print(f"{target} 不存在，跑 `make schema` 生成", file=sys.stderr)
            return 1
        if target.read_text(encoding="utf-8") != rendered:
            print(
                f"{target} 和 TaskDefinition 模型对不上了。\n改了模型就要重新导出：make schema",
                file=sys.stderr,
            )
            return 1
        print(f"{target} 是最新的")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    print(f"已写出 {target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cli.task", description="题目的导入/导出")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="校验题目文件（.json / .jsonl）")
    p_import.add_argument("paths", nargs="+")
    p_import.set_defaults(func=cmd_import)

    p_export = sub.add_parser("export", help="规范化并回填 content_hash")
    p_export.add_argument("path")
    p_export.add_argument("--out", help="不给就打到标准输出")
    p_export.set_defaults(func=cmd_export)

    p_schema = sub.add_parser("schema", help="导出 JSON Schema")
    p_schema.add_argument("--out", help=f"默认 {DEFAULT_SCHEMA_PATH}")
    p_schema.add_argument("--check", action="store_true", help="只检查是否最新，不写文件")
    p_schema.set_defaults(func=cmd_schema)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
