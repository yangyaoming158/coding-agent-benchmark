"""Makefile 里两条"看不见就会咬人"的约定（#88、#89）。

两条都不是代码逻辑，是 Makefile 的写法，但出错的表现都在别处：
一条把实验名写成主机名，一条把开发库清空。放在单元测试里是因为它们
只要读一遍 Makefile 就能验，不需要数据库也不需要 Docker。
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path

from tests.integration.conftest import warn_if_this_is_not_a_test_database

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
    for name in ("AGENT", "NAME", "DATASET", "SLUG", "MINE_REPO", "TEST_DATABASE_URL"):
        assert assignment(name) == ":=", f"{name} 用了 {assignment(name)}，会被同名环境变量盖掉"


def test_test_targets_point_at_a_separate_database() -> None:
    """跑测试的目标必须指向独立的测试库，而且库名里要带 test。

    跑任何一个集成测试都会 `downgrade base` + `upgrade head`。指向开发库的话，
    `make check` 会把挖好的候选和验完的题一起清掉，重灌一遍二十分钟（#88）。
    """
    url = re.search(r"^TEST_DATABASE_URL\s*:=\s*(\S+)", MAKEFILE, re.MULTILINE)
    assert url is not None
    assert "test" in url.group(1).rsplit("/", 1)[-1].lower()

    for target in ("test", "test-docker", "test-agent", "test-all"):
        body = re.search(rf"^{target}:[^\n]*\n((?:\t[^\n]*\n)+)", MAKEFILE, re.MULTILINE)
        assert body is not None, f"Makefile 里没有 {target} 目标"
        assert "$(UV_TEST)" in body.group(1), f"{target} 没走 UV_TEST，会跑在开发库上"


def test_a_database_without_test_in_its_name_gets_a_warning() -> None:
    """库名里不含 test 就警告 —— 默认隔离之后，"集成测试会清库"这件事仍然说得出来。

    警告不拒绝：CI 的库名未必叫 bench_test，一刀切会把别人的流水线挡死。
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert warn_if_this_is_not_a_test_database("postgresql://u:p@h:5433/bench_test") is None
        assert warn_if_this_is_not_a_test_database("postgresql://u:p@h:5433/bench") is not None


def test_a_bare_pytest_run_gets_redirected_to_the_test_database() -> None:
    """没给 `BENCH_DATABASE_URL` 时，根 conftest 把库名换成带 `_test` 的那个。

    #88 只让 Makefile 指向测试库，绕开 Makefile 直接 `uv run pytest` 还是连开发库，
    靠的是一条警告。**警告不够**：2026-09-12 一天之内被这条路清了两次库
    （E9-T2 开工时一次、收尾时一次），两次都是手快敲了 `uv run pytest`。
    所以改成安全默认。
    """
    from tests.conftest import _switch_to_test_db

    assert (
        _switch_to_test_db("postgresql+psycopg://bench:bench@localhost:5433/bench")
        == "postgresql+psycopg://bench:bench@localhost:5433/bench_test"
    )
    # 带查询参数的连接串不能把参数吞掉
    assert (
        _switch_to_test_db("postgresql+psycopg://u:p@h:5432/app?sslmode=require")
        == "postgresql+psycopg://u:p@h:5432/app_test?sslmode=require"
    )
