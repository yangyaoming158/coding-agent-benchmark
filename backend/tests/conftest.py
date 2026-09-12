"""整个测试树的根 conftest：**没显式指库的时候，自动改指测试库**。

## 为什么要这一层

跑任何一个集成测试都会 `downgrade base` + `upgrade head`，把它连上的库整个抹掉。
#88 已经让 `make test` / `make check` 指向 `bench_test`，开发库不受影响。

但那只覆盖"从 Makefile 跑"这一条路。直接敲

    cd backend && uv run pytest

连的还是开发库，一条集成测试就把它清了。#88 在 `tests/integration/conftest.py` 里
留了一条醒目的警告，**但警告不够** —— 2026-09-12 一天之内被这条路清了两次，
两次都是手快敲了 `uv run pytest`。警告在 pytest 的汇总区，等看见的时候库已经没了。

所以这里改成**安全默认**：环境变量没显式给的时候，自动把库名换成带 `_test` 的那个。
显式给了就一个字不动（`make test` 给的是 `bench_test`，CI 给的是它自己的库）。

## 为什么在 `pytest_configure` 里改环境变量，而不是改配置对象

测试里有一批用例会起 CLI 子进程（`cli.dataset` / `cli.experiment` 自己开 engine）。
只改进程内的 settings 的话，子进程读的还是原来那个库 —— 一半落测试库、
一半落开发库，比全落开发库更难查。环境变量会被子进程继承。
"""

from __future__ import annotations

import os

import pytest

#: 库名里带这个词就认为它是测试库。和 `tests/integration/conftest.py` 的判据一致。
TEST_DB_MARKER = "test"

ENV_NAME = "BENCH_DATABASE_URL"


def _switch_to_test_db(url: str) -> str:
    """把连接串里的库名换成带 `_test` 的。`.../bench` → `.../bench_test`。"""
    head, _, name = url.rpartition("/")
    name, sep, query = name.partition("?")
    return f"{head}/{name}_test{sep}{query}"


def pytest_configure(config: pytest.Config) -> None:
    """没显式指库就改指测试库，并把这件事打出来。

    `config` 用不上，但 pytest 的钩子签名要求带着它。
    """
    if os.environ.get(ENV_NAME):
        return  # 显式给了就听调用方的

    from app.infrastructure.config import DEFAULT_DATABASE_URL, reset_settings_cache

    if TEST_DB_MARKER in DEFAULT_DATABASE_URL.rpartition("/")[2].lower():
        return  # 默认值本来就是测试库（别的部署可能这么配）

    url = _switch_to_test_db(DEFAULT_DATABASE_URL)
    os.environ[ENV_NAME] = url
    reset_settings_cache()
    print(f"\n没给 {ENV_NAME}，自动改指测试库 {url}（库不存在就跑 make db-test）")
