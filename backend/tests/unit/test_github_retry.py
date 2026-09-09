"""`gh` 调用失败时哪些该重试、哪些该当场认输（E1-T4 补，模块是 E8-T1 的）。

这一层分错了代价很不对称：

- **该重试的没重试** → 一次偶发就把整个仓库的挖掘作业打断，
  前面翻的几十页和烧掉的配额一起作废。
- **不该重试的重试了** → 白等 5+15+45+90 秒，然后报同一个错。

所以判据是"这是 GitHub 那边的问题，还是我们查询写错了"。查询写错了重试多少次
都一样，要当场把错误抛出来让人去改查询。
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from app.benchmark import github
from app.benchmark.github import GitHubError, GitHubNotFoundError


class FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """退避是真的 sleep 5/15/45/90 秒。测试里不等，只验重试了几次。"""
    monkeypatch.setattr(github.time, "sleep", lambda _s: None)


def fake_runs(monkeypatch: pytest.MonkeyPatch, results: list[Any]) -> list[list[str]]:
    """按顺序返回预设结果，并记下每次调用的参数。结果是异常就抛出来。"""
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> FakeCompleted:
        calls.append(args)
        outcome = results[min(len(calls) - 1, len(results) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(github.subprocess, "run", run)
    return calls


def test_githubs_own_hiccup_is_retried(monkeypatch: pytest.MonkeyPatch, no_waiting: None) -> None:
    """GitHub 偶发的 "Something went wrong while executing your query"。

    **不是**我们查询写错了：同一条查询隔几秒重发就过（2026-09-09 实测，
    E1-T4 挖 pallets/click 第一页就撞上一次）。不重试的话，一次偶发
    就把这个仓库的整轮挖掘打断。
    """
    calls = fake_runs(
        monkeypatch,
        [
            FakeCompleted(
                1,
                stderr=(
                    "gh: Something went wrong while executing your query "
                    "on 2026-09-09T14:00:41Z. Please include `CCB6:34B43` when reporting."
                ),
            ),
            FakeCompleted(0, stdout='{"data": {"ok": true}}'),
        ],
    )
    assert github.rest("repos/pallets/click") == {"data": {"ok": True}}
    assert len(calls) == 2, "第一次失败之后应该重试一次就成功"


def test_secondary_rate_limit_is_retried(monkeypatch: pytest.MonkeyPatch, no_waiting: None) -> None:
    """次级限流（短时间请求太密）。挖几十个仓库时撞上它是常态。"""
    calls = fake_runs(
        monkeypatch,
        [
            FakeCompleted(1, stderr="You have exceeded a secondary rate limit"),
            FakeCompleted(0, stdout="{}"),
        ],
    )
    github.rest("repos/x/y")
    assert len(calls) == 2


def test_a_broken_query_fails_immediately(
    monkeypatch: pytest.MonkeyPatch, no_waiting: None
) -> None:
    """查询写错了重试多少次都一样，要当场抛出来让人去改查询。"""
    calls = fake_runs(
        monkeypatch,
        [FakeCompleted(1, stderr="Field 'nosuchfield' doesn't exist on type 'PullRequest'")],
    )
    with pytest.raises(GitHubError, match="nosuchfield"):
        github.rest("graphql")
    assert len(calls) == 1, "不该重试"


def test_a_missing_repo_is_a_conclusion_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, no_waiting: None
) -> None:
    """仓库不存在是一条结论，不是故障。调用方按它跳过这个仓库、继续下一个。"""
    calls = fake_runs(
        monkeypatch, [FakeCompleted(1, stderr="Could not resolve to a Repository with the name")]
    )
    with pytest.raises(GitHubNotFoundError):
        github.rest("repos/nobody/nothing")
    assert len(calls) == 1


def test_a_timeout_comes_out_as_a_githuberror(
    monkeypatch: pytest.MonkeyPatch, no_waiting: None
) -> None:
    """超时也要包成 `GitHubError`，不能让 `subprocess.TimeoutExpired` 冒出去。

    调用方只 catch `GitHubError`，漏一种异常类型就等于"第 13 个仓库慢了一次，
    前 12 个的结果连同烧掉的配额一起没了"—— 2026-09-08 探大仓库时真这么翻过一次。
    """
    calls = fake_runs(monkeypatch, [subprocess.TimeoutExpired(cmd="gh", timeout=120)])
    with pytest.raises(GitHubError, match="秒没返回"):
        github.rest("repos/x/y")
    assert len(calls) == github.MAX_RETRIES + 1, "超时按可重试处理，重试到上限"


def test_retrying_gives_up_after_the_limit(
    monkeypatch: pytest.MonkeyPatch, no_waiting: None
) -> None:
    """一直失败也要停。退避表是 5/15/45/90 秒，无限重试等于挂住一整个作业。"""
    calls = fake_runs(monkeypatch, [FakeCompleted(1, stderr="503 Service Unavailable")])
    with pytest.raises(GitHubError):
        github.rest("repos/x/y")
    assert len(calls) == github.MAX_RETRIES + 1
