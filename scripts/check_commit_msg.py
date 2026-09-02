#!/usr/bin/env python3
"""检查提交信息是否符合 Conventional Commits（AGENTS.md 第 7 节）。

格式：<类型>(<Epic 编号>): <描述>
例：  feat(E2): 容器执行器支持 pids-limit 与 OOM 判定
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

模式 = re.compile(r"^(feat|fix|test|docs|refactor|chore|perf|revert)(\([\w./-]+\))?!?: .{1,}")
豁免 = ("Merge ", "Revert ", "fixup!", "squash!")


def main(argv: list[str]) -> int:
    首行 = Path(argv[1]).read_text(encoding="utf-8").splitlines()[0].strip()
    if not 首行 or 首行.startswith("#") or 首行.startswith(豁免):
        return 0
    if 模式.match(首行):
        return 0
    print(
        f"提交信息不符合规范：\n  {首行}\n\n"
        "格式：<类型>(<Epic 编号>): <描述>\n"
        "类型：feat / fix / test / docs / refactor / chore / perf / revert\n"
        "例：  feat(E2): 容器执行器支持 pids-limit 与 OOM 判定\n"
        "      docs(plan): 回填沙箱实测结论\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
