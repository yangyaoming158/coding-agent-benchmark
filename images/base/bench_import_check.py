#!/usr/bin/env python3
"""建 env 镜像时的自查工具：`import X` 拿到的到底是哪一份代码？（E2-T3）

    bench-import-check --root /workspace sqlfluff

这个问题不是学术问题。env 镜像里躺着一份仓库快照（`/opt/repo`，装依赖要用它），
而评测时挂进来的工作区（`/workspace`）是另一个 commit，还被被测 AI 改过。
如果 `import sqlfluff` 解析到了镜像里那一份，**被测 AI 的改动根本不会被执行** ——
测试照样能跑，甚至可能全绿，而 Oracle 哨兵会从 100% 悄悄掉下去，日志里一点异常都没有。

平铺布局（包目录直接在仓库根）碰巧没事：pytest 把 rootdir 放进 sys.path 最前面。
src 布局就会中招：`/workspace` 底下压根没有 `sqlfluff` 这个名字，它在 `src/sqlfluff`。

所以 bench-env 建完之后跑一次这个脚本，解析结果不在 `--root` 底下就让构建当场失败。
和 E2-T1「物化完自查树哈希」是同一个套路：宁可建镜像时报错，也不要跑完
300 次评测才发现补丁没生效。

退出码：0 全部通过；1 有模块解析到了 root 之外；2 有模块 import 不了。
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys


def resolve(name: str) -> str:
    """import 一个模块，回答"它的代码在哪"。

    先看 `__file__`；命名空间包没有这个属性，退回 `__path__` 的第一项。
    两个都没有（内置模块）就返回一个说明字符串，让调用方按"不在 root 底下"处理。
    """
    module = importlib.import_module(name)
    path = getattr(module, "__file__", None)
    if path:
        return os.path.realpath(path)
    search = list(getattr(module, "__path__", []) or [])
    if search:
        return os.path.realpath(search[0])
    return f"<内置模块，没有文件路径：{name}>"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查模块解析到哪个目录")
    parser.add_argument("--root", required=True, help="模块必须解析到这个目录底下")
    parser.add_argument("modules", nargs="+", help="要检查的模块名")
    args = parser.parse_args(argv)

    root = os.path.realpath(args.root)
    failures = 0
    import_errors = 0

    for name in args.modules:
        try:
            where = resolve(name)
        except Exception as exc:
            print(f"✗ {name}：import 失败 —— {type(exc).__name__}: {exc}")
            import_errors += 1
            continue
        # 用 os.path.commonpath 而不是 startswith：`/workspace-old` 也以 `/workspace`
        # 开头，字符串前缀判等于把它当成通过了
        inside = os.path.isabs(where) and os.path.commonpath([where, root]) == root
        print(f"{'✓' if inside else '✗'} {name} → {where}")
        if not inside:
            failures += 1

    if import_errors:
        print(f"\n{import_errors} 个模块 import 不了。依赖没装全，或者模块名写错了。")
        return 2
    if failures:
        print(
            f"\n{failures} 个模块解析到了 {root} 之外，也就是解析到了镜像里那份快照。\n"
            "评测时被测 AI 改的是工作区里的代码，而跑的是镜像里的 —— 补丁不会生效。\n"
            "改法：在 env 配方里把包的源码目录写进 workspace_source_roots"
            '（src 布局写 "src"），或者把 install_steps 换成 '
            "`--config-settings editable_mode=compat` 的装法。"
        )
        return 1

    print(f"\n全部 {len(args.modules)} 个模块都解析到了 {root} 底下。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
