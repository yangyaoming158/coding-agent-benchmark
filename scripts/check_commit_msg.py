#!/usr/bin/env python3
"""检查提交信息是否符合 Conventional Commits（AGENTS.md 第 7 节）。

格式：<类型>(<Epic 编号>): <描述>
例：  feat(E2): 容器执行器支持 pids-limit 与 OOM 判定
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SUBJECT_PATTERN = re.compile(
    r"^(feat|fix|test|docs|refactor|chore|perf|revert)(\([\w./-]+\))?!?: .{1,}"
)
# 这些是 git 自己生成的提交信息，不该被规范卡住
EXEMPT_PREFIXES = ("Merge ", "Revert ", "fixup!", "squash!")


def is_valid_subject(subject: str) -> bool:
    """判断提交信息首行合不合规。空行和注释行视为合规，交给 git 自己处理。"""
    subject = subject.strip()
    if not subject or subject.startswith("#") or subject.startswith(EXEMPT_PREFIXES):
        return True
    return SUBJECT_PATTERN.match(subject) is not None


def main(argv: list[str]) -> int:
    lines = Path(argv[1]).read_text(encoding="utf-8").splitlines()
    subject = lines[0].strip() if lines else ""
    if is_valid_subject(subject):
        return 0
    print(
        f"提交信息不符合规范：\n  {subject}\n\n"
        "格式：<类型>(<Epic 编号>): <描述>\n"
        "类型：feat / fix / test / docs / refactor / chore / perf / revert\n"
        "例：  feat(E2): 容器执行器支持 pids-limit 与 OOM 判定\n"
        "      docs(plan): 回填沙箱实测结论\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
