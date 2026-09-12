"""Makefile 里"看不见就会咬人"的约定（#89）。

不是代码逻辑，是 Makefile 的写法，但出错的表现在别处：实验名被写成主机名。
放在单元测试里是因为读一遍 Makefile 就能验，不需要数据库也不需要 Docker。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")


def assignment(name: str) -> str:
    """取 Makefile 里给 `name` 赋值那一行用的是哪种赋值号。"""
    match = re.search(rf"^{name}\s*(\?=|:=|=)", MAKEFILE, re.MULTILINE)
    assert match is not None, f"Makefile 里没有给 {name} 赋值的行"
    return match.group(1)


def test_defaults_do_not_use_question_mark_assignment() -> None:
    """默认参数一律用 `:=`，不用 `?=`。

    `?=` 是"没定义才赋值"，而 make 把**环境变量也算已定义**。WSL 里恰好有个
    叫 `NAME` 的环境变量（Windows 侧透过来的计算机名），于是 `NAME ?= adhoc`
    建出来的实验名是 `DESKTOP-D3QQNH3`，实验列表里一排主机名（#89）。

    `:=` 不看环境变量，而命令行赋值仍然优先，所以 `make enqueue NAME=x` 不受影响。
    """
    for name in ("AGENT", "NAME", "DATASET", "SLUG", "MINE_REPO"):
        assert assignment(name) == ":=", f"{name} 用了 {assignment(name)}，会被同名环境变量盖掉"
