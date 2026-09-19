"""自建题的中文题面（§8.5 Plan B，`cli/localize.py`）：纯函数部分，不碰库。

三件事要挡住：
1. 对照表里结论不是 ACCEPT 的一律不导，填错的当场报错而不是静默跳过；
2. 改写后的题只换题面（标题、正文、语言、标签），测试和补丁一个字不动，哈希重算；
3. 把英文原样贴进 zh 列、或者只写一句话，不算中文题面。
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from cli.localize import (
    REWRITTEN_TAG,
    LocalizedIssue,
    LocalizeError,
    check_row,
    localize_definition,
    looks_chinese,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "sample_task.json"

ZH_BODY = (
    "用 `click.Choice` 定义一个必填参数之后，`--help` 里显示出来的形式让人误以为它是可选的。"
    "按命令行帮助的常见约定，方括号表示可选参数、尖括号表示必填参数，而现在 Choice 参数不管"
    "是不是必填，帮助信息里都是同一副样子。复现步骤：定义一个 `required=True` 的 Choice 选项，"
    "运行 `--help`，观察用法行。预期是必填参数不该被方括号括起来。环境：Python 3.12，Click 8.3.1。"
    "\n\n```python\n@click.option('--mode', type=click.Choice(['a', 'b']), required=True)\n```\n"
)


def a_row(**overrides: Any) -> dict[str, str]:
    row = {
        "task_id": "nonebot__nonebot2-2314",
        "zh_title": "Choice 参数在帮助里看起来像可选的",
        "zh_body": ZH_BODY,
        "reviewer": "yym",
        "verdict": "ACCEPT",
        "note": "",
    }
    row.update(overrides)
    return row


# ── check_row ───────────────────────────────────────────────


def test_blank_or_reject_rows_are_skipped_not_errors() -> None:
    assert check_row(a_row(verdict="")) is None
    assert check_row(a_row(verdict="reject")) is None


def test_unknown_verdict_is_an_error() -> None:
    with pytest.raises(LocalizeError, match="verdict 只能填"):
        check_row(a_row(verdict="MAYBE"))


def test_accept_needs_title_body_and_reviewer() -> None:
    with pytest.raises(LocalizeError, match="有空的"):
        check_row(a_row(zh_body=""))
    with pytest.raises(LocalizeError, match="reviewer 没填"):
        check_row(a_row(reviewer=""))


def test_short_body_is_rejected() -> None:
    """短于 MIN_ISSUE_BODY_CHARS 的题面会被路由到 REVIEW_REQUIRED，导进去也发不了，不如当场挡住。"""
    with pytest.raises(LocalizeError, match="少于 200"):
        check_row(a_row(zh_body="太短了，" * 10))


def test_english_pasted_into_zh_column_is_rejected() -> None:
    english = (
        "Creating a required parameter with a Choice type leads to confusing helpstrings. " * 5
    )
    with pytest.raises(LocalizeError, match="汉字"):
        check_row(a_row(zh_body=english))


def test_valid_row_becomes_a_localized_issue() -> None:
    issue = check_row(a_row(verdict=" accept ", note="改写自 #1272"))
    assert issue == LocalizedIssue(
        task_id="nonebot__nonebot2-2314",
        zh_title="Choice 参数在帮助里看起来像可选的",
        zh_body=ZH_BODY.strip(),
        reviewer="yym",
        note="改写自 #1272",
    )


# ── looks_chinese ───────────────────────────────────────────


def test_code_blocks_do_not_count_against_chinese() -> None:
    """正文几乎全是照抄的回溯和代码，代码块之外那几句中文就够了。"""
    body = (
        "命令里用了 `echo_via_pager` 之后 `CliRunner.invoke` 会报错，复现代码和回溯如下。\n"
        + ("```\n" + 'Traceback (most recent call last):\n  File "x.py", line 1\n' * 30 + "```\n")
        + "环境：Ubuntu 24.04，Python 3.13.5，Click 8.4。预期是不报错。"
        + "这个问题从 8.4 开始出现，8.3.3 上没有。"
    )
    assert looks_chinese(body) is None


def test_one_chinese_sentence_after_english_is_not_chinese() -> None:
    body = (
        "When echo_via_pager is used in the command, CliRunner.invoke fails with a ValueError. " * 6
    ) + "以上是问题。"
    assert looks_chinese(body) is not None


# ── localize_definition ─────────────────────────────────────


def load_fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def test_localized_definition_changes_only_the_issue_side() -> None:
    raw = load_fixture()
    issue = check_row(a_row())
    assert issue is not None
    localized = localize_definition(raw, issue)

    assert localized.issue_title == issue.zh_title
    assert localized.issue_body == issue.zh_body
    assert localized.issue_language.value == "zh"
    assert localized.hints_text is None
    assert REWRITTEN_TAG in localized.tags
    assert localized.content_hash != raw["content_hash"]
    assert localized.content_hash and localized.content_hash.startswith("sha256:")

    before = deepcopy(raw)
    after = localized.model_dump(mode="json")
    for key in (
        "fail_to_pass",
        "pass_to_pass",
        "test_patch",
        "gold_patch",
        "base_commit",
        "environment_id",
        "test_command",
        "test_timeout_s",
        "agent_timeout_s",
        "validation",
    ):
        assert after[key] == before[key], key
    assert set(after["tags"]) == set(before["tags"]) | {REWRITTEN_TAG}


def test_localized_definition_is_idempotent() -> None:
    """同一份中文题面导两遍得到同一个哈希 —— 重灌时可以放心重跑。"""
    raw = load_fixture()
    issue = check_row(a_row())
    assert issue is not None
    once = localize_definition(raw, issue)
    twice = localize_definition(once.model_dump(mode="json"), issue)
    assert once.content_hash == twice.content_hash


def test_leaking_body_is_rejected_by_the_schema() -> None:
    """泄题检查不在这里另写一套，靠 TaskDefinition 自己的校验器。"""
    raw = load_fixture()
    leak = ZH_BODY + "\n修法见 https://github.com/pallets/click/pull/3578 。"
    issue = check_row(a_row(zh_body=leak))
    assert issue is not None
    with pytest.raises(LocalizeError, match="不合法"):
        localize_definition(raw, issue)


def test_task_id_mismatch_is_an_error() -> None:
    raw = load_fixture()
    issue = check_row(a_row(task_id="pallets__click-1"))
    assert issue is not None
    with pytest.raises(LocalizeError, match="task_id"):
        localize_definition(raw, issue)
