"""Runner 协议的 JSON Schema 导出与报文校验（E3-T1）。

    python -m cli.runner schema                       # 写出两份 schema
    python -m cli.runner schema --check               # CI 用：文件和模型对不上就非零退出
    python -m cli.runner validate-input task.json     # 校验一份任务输入，顺带查泄题
    python -m cli.runner validate-result out.log      # 从适配器的整段 stdout 里读结果

**为什么要把 schema 导出来**：写适配器的人不一定用 Python。Schema 是对外的数据契约，
让别人照着实现一个适配器，而不用去读我们的 Pydantic 模型。

`validate-result` 收的是**整段 stdout**，不是那一行 JSON —— 排查"适配器结果读不出来"
时，手边有的正是那一坨刷屏日志，让人先自己把最后一行找出来是多余的一步。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ValidationError

from app.runner.protocol import (
    AgentRunResult,
    AgentTaskInput,
    ProtocolError,
    assert_no_leak,
    parse_result_stdout,
)

#: 两份 schema 的落盘位置（仓库根的 schemas/）。和 `task.schema.json` 放一起：
#: 它们都是"别人要照着做"的对外契约。
SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"
INPUT_SCHEMA_PATH = SCHEMA_DIR / "agent-task-input.schema.json"
RESULT_SCHEMA_PATH = SCHEMA_DIR / "agent-run-result.schema.json"


def render_schema(model: type[BaseModel]) -> str:
    """把 Pydantic 模型导成 JSON Schema 文本。

    `sort_keys=True` 是必需的：不排序的话同一个模型两次导出的键序可能不同，
    每次跑都产生一份"有改动"的文件，漂移检查就失去意义了。
    """
    return (
        json.dumps(model.model_json_schema(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    )


def schema_targets() -> list[tuple[Path, str]]:
    """要落盘的 (路径, 内容) 对。"""
    return [
        (INPUT_SCHEMA_PATH, render_schema(AgentTaskInput)),
        (RESULT_SCHEMA_PATH, render_schema(AgentRunResult)),
    ]


def cmd_schema(args: argparse.Namespace) -> int:
    """导出两份 JSON Schema；`--check` 只比对不写。"""
    stale: list[Path] = []
    for target, rendered in schema_targets():
        if args.check:
            if not target.exists() or target.read_text(encoding="utf-8") != rendered:
                stale.append(target)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
        print(f"已写出 {target}")

    if not args.check:
        return 0
    if stale:
        names = "、".join(p.name for p in stale)
        print(f"{names} 和模型对不上了。改了模型就要重新导出：make schema", file=sys.stderr)
        return 1
    print("两份 schema 都是最新的")
    return 0


def cmd_validate_input(args: argparse.Namespace) -> int:
    """校验一份任务输入 JSON。

    除了字段合不合法，还会走一遍泄题检查（协议 C-76）—— 这正是这条命令存在的理由：
    人工造调试用的任务输入时，顺手把 `fail_to_pass` 粘进去是很容易发生的事。
    """
    path = Path(args.path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"{path}: 读不出 JSON —— {exc}", file=sys.stderr)
        return 1

    if not isinstance(payload, dict):
        print(f"{path}: 顶层必须是一个 JSON 对象", file=sys.stderr)
        return 1

    try:
        assert_no_leak(payload)
        task = AgentTaskInput.model_validate(payload)
    except (ProtocolError, ValidationError) as exc:
        print(f"{path}: 不合协议 —— {exc}", file=sys.stderr)
        return 1

    print(f"{path}: 合法（task_id={task.task_id}，模型 {task.model.name}）")
    return 0


def cmd_validate_result(args: argparse.Namespace) -> int:
    """从适配器的整段 stdout 里读出结果并校验。"""
    path = Path(args.path)
    try:
        stdout = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"{path}: 读不了 —— {exc}", file=sys.stderr)
        return 1

    try:
        result = parse_result_stdout(stdout)
    except ProtocolError as exc:
        print(f"{path}: 读不出结果 —— {exc}", file=sys.stderr)
        return 1

    patch_note = f"{len(result.patch)} 字节补丁" if result.has_patch else "空补丁"
    print(f"{path}: 合法（agent={result.agent_name}，{patch_note}，成本来源 {result.cost_source}）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cli.runner", description="Runner 协议的 schema 与报文校验"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_schema = sub.add_parser("schema", help="导出两份 JSON Schema")
    p_schema.add_argument("--check", action="store_true", help="只检查是否最新，不写文件")
    p_schema.set_defaults(func=cmd_schema)

    p_input = sub.add_parser("validate-input", help="校验任务输入 JSON（含泄题检查）")
    p_input.add_argument("path")
    p_input.set_defaults(func=cmd_validate_input)

    p_result = sub.add_parser("validate-result", help="从适配器 stdout 里读结果并校验")
    p_result.add_argument("path")
    p_result.set_defaults(func=cmd_validate_result)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
