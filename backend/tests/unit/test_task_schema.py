"""题目 Schema 的测试（E1-T1）。

两条验收标准：

1. Golden Task 的 JSON 能双向序列化，`content_hash` 对字段序不敏感 → 见 `test_content_hash.py`
2. **非法任务被明确拒绝并给出可读原因** → 本文件的重点

第 2 条为什么重要：坏题比没题更糟。一道 F2P 在修复前就通过的题，会让每个 AI 都
"答对"，解决率无声地偏高；一道 gold_patch 修不好的题，会让每个 AI 都"答错"。
两种都不报错，只是让排行榜失真，而且极难查——所以能在导入时挡掉的，一条都不能漏。
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.benchmark.schema import MIN_ISSUE_BODY_CHARS, TaskDefinition

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "sample_task.json"


def load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def make_task(**overrides: Any) -> dict[str, Any]:
    """基于 Golden 样例改几个字段，造一道题出来。

    默认去掉 `content_hash` 和 `validation`：改了任何字段之后原来那个哈希必然对不上，
    留着的话每个用例都会先撞上"哈希对不上"，看不到真正想测的那条规则。
    """
    task = deepcopy(load_fixture())
    task.pop("content_hash", None)
    task.pop("validation", None)
    task.update(overrides)
    return task


def reject_reason(**overrides: Any) -> str:
    """构造一道坏题，返回报错消息。没被拒收就让测试失败。"""
    with pytest.raises(ValidationError) as excinfo:
        TaskDefinition.model_validate(make_task(**overrides))
    return str(excinfo.value)


# ── 先确认样例本身是好的 ────────────────────────────────────


def test_golden_fixture_is_valid() -> None:
    """样例题连 content_hash 和 validation 一起验，全部通过。

    这条同时验了"声明的哈希能被重算出来"——文件里那个哈希不是随手填的。
    """
    task = TaskDefinition.model_validate(load_fixture())
    assert task.task_id == "nonebot__nonebot2-2314"
    assert task.review_flags() == []
    assert task.validation is not None
    assert task.validation.state.value == "VALID"


def test_roundtrip_is_lossless() -> None:
    """JSON → 模型 → JSON，再解析一次，结果完全一致。"""
    original = TaskDefinition.model_validate(load_fixture())
    dumped = original.model_dump(mode="json")
    again = TaskDefinition.model_validate(dumped)
    assert again.model_dump(mode="json") == dumped
    assert again.content_hash == original.content_hash


# ══════════════════════════════════════════════════════════
# 非法任务：每一条都对应一种已知的坏题或作弊路径
# ══════════════════════════════════════════════════════════


def test_reject_short_base_commit() -> None:
    """base_commit 必须是 40 位全 SHA（§7.2(2)）。

    短 SHA 会碰撞，分支名和 tag 会移动——它们都不能唯一确定一份代码，
    "可复现"就成了空话。
    """
    reason = reject_reason(base_commit="3f2a1c9")
    assert "base_commit" in reason
    assert "40 位" in reason


@pytest.mark.parametrize(
    ("bad_id", "why"),
    [
        ("nonebot2-2314", "缺少 owner__repo 分隔"),
        ("nonebot__nonebot2", "缺少 PR 号"),
        ("nonebot__nonebot2-abc", "PR 号不是数字"),
    ],
)
def test_reject_malformed_task_id(bad_id: str, why: str) -> None:
    """task_id 必须是 `{owner}__{repo}-{pr_number}`，与 SWE-bench 命名兼容。"""
    assert "task_id" in reject_reason(task_id=bad_id), why


def test_reject_empty_fail_to_pass() -> None:
    """F2P 为空的题无法判定（§7.2(5)）。

    没有"修好才会通过"的测试，就没有任何东西能证明 AI 修好了。
    """
    assert "fail_to_pass" in reject_reason(fail_to_pass=[])


def test_reject_overlapping_f2p_and_p2p() -> None:
    """同一个用例不能既是 F2P 又是 P2P——它不可能既"修复前必失败"又"修复前必通过"。"""
    shared = "tests/test_adapter.py::test_basic_register"
    reason = reject_reason(fail_to_pass=[shared], pass_to_pass=[shared])
    assert "既在 fail_to_pass" in reason


def test_reject_test_patch_touching_source() -> None:
    """test_patch 只能改测试文件（§7.1）。

    碰了业务代码的话，官方测试补丁自己就把 bug 修了一部分，
    F2P 在 base 上可能就通过了，这道题失去区分度。
    """
    patch = (
        "diff --git a/nonebot/adapter.py b/nonebot/adapter.py\n"
        "--- a/nonebot/adapter.py\n"
        "+++ b/nonebot/adapter.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    reason = reject_reason(test_patch=patch, test_patch_paths=[])
    assert "test_patch 只能改测试文件" in reason
    assert "nonebot/adapter.py" in reason


def test_reject_tampered_test_patch_paths() -> None:
    """手工改 test_patch_paths 想放开某个文件的保护 —— 重算一遍就对不上（协议 C-74 第 6 条）。

    这份清单会被并进受保护路径。有人把某个测试文件从清单里删掉，
    那个文件就不再被还原，AI 改它就生效了。所以导入时必须重算比对。
    """
    reason = reject_reason(test_patch_paths=["tests/无关文件.py"])
    assert "C-74" in reason
    assert "tests/test_adapter.py" in reason


def test_reject_gold_patch_hitting_protected_path() -> None:
    """gold_patch 命中受保护路径 → 题目无效（协议 C-64）。

    官方补丁要是也改测试，那"修好"的判据就被官方补丁自己动了手脚。
    """
    patch = (
        "diff --git a/tests/conftest.py b/tests/conftest.py\n"
        "--- a/tests/conftest.py\n"
        "+++ b/tests/conftest.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    reason = reject_reason(gold_patch=patch)
    assert "C-64" in reason
    assert "tests/conftest.py" in reason


def test_reject_empty_gold_patch() -> None:
    """只改测试就能通过的题要丢掉（§7.2(7)）——它考的不是修 bug。"""
    assert "gold_patch 为空" in reject_reason(gold_patch="")


@pytest.mark.parametrize(
    ("leak", "why"),
    [
        ("修复见 https://github.com/nonebot/nonebot2/pull/2314 ", "PR 链接"),
        ("diff --git a/nonebot/adapter.py b/nonebot/adapter.py\n-old\n+new\n", "贴了 diff"),
    ],
)
def test_reject_issue_body_leaking_answer(leak: str, why: str) -> None:
    """issue 正文里不能带着答案（§7.2(7)）。

    把 PR 链接留在 issue 里，被测 AI 顺着链接就能看到官方修复——
    这时候测的是"会不会点链接"，不是"会不会修 bug"。
    """
    body = load_fixture()["issue_body"] + "\n\n" + leak
    assert "§7.2(7)" in reject_reason(issue_body=body), why


def test_reject_wrong_content_hash() -> None:
    """声明的哈希和重算的对不上 → 拒收。题目被改过，或者哈希算错了。"""
    task = load_fixture()
    task["content_hash"] = "sha256:" + "0" * 64
    with pytest.raises(ValidationError, match="content_hash 对不上"):
        TaskDefinition.model_validate(task)


def test_reject_unknown_field() -> None:
    """多一个不认识的字段就报错。

    宽容处理的代价是字段名拼错不报错：`fail_to_pass` 写成 `failed_to_pass`，
    题目会变成"没有 F2P"——而那本该是拒收条件，却因为拼写错误绕过去了。
    """
    with pytest.raises(ValidationError, match="failed_to_pass"):
        TaskDefinition.model_validate(make_task(failed_to_pass=["x"]))


def test_reject_malformed_test_patch() -> None:
    """test_patch 不是合法 diff（比如被截断了）→ 解析不出路径，拒收。"""
    reason = reject_reason(test_patch="这不是一个补丁", test_patch_paths=[])
    assert "不是合法的 unified diff" in reason


# ══════════════════════════════════════════════════════════
# 需要人工复核（合法但可疑），和硬性拒收分开
# ══════════════════════════════════════════════════════════


def test_short_issue_is_flagged_not_rejected() -> None:
    """issue 太短要人工看一眼，但不是拒收理由（§7.2(7)、§7.4）。

    有些 issue 确实短，但配了清晰的复现步骤。一律拒收会误伤好题；
    一律放行又会混进"不工作"这种没信息量的题。所以走人工队列。
    """
    task = TaskDefinition.model_validate(make_task(issue_body="重连之后事件重复了。"))
    flags = task.review_flags()
    assert any(str(MIN_ISSUE_BODY_CHARS) in flag for flag in flags)


def test_missing_p2p_is_flagged() -> None:
    """没有 P2P 就没有回归护栏：AI 删掉相关功能也能让 F2P 通过（§7.2(6)）。"""
    task = TaskDefinition.model_validate(make_task(pass_to_pass=[]))
    assert any("回归护栏" in flag for flag in task.review_flags())


def test_too_many_f2p_is_flagged() -> None:
    """F2P 太多说明这道题一次改了太多东西，难度和归因都会失真（§7.4）。"""
    task = TaskDefinition.model_validate(
        make_task(fail_to_pass=[f"tests/test_adapter.py::test_{i}" for i in range(25)])
    )
    assert any("超过" in flag for flag in task.review_flags())


# ══════════════════════════════════════════════════════════
# 规范化
# ══════════════════════════════════════════════════════════


def test_collections_are_sorted_and_deduped() -> None:
    """集合语义的列表排序去重——这是哈希对顺序不敏感的另一半。"""
    task = TaskDefinition.model_validate(
        make_task(
            fail_to_pass=[
                "tests/test_adapter.py::test_b",
                "tests/test_adapter.py::test_a",
                "tests/test_adapter.py::test_b",
            ],
            pass_to_pass=[],
            tags=["zebra", "alpha", "alpha"],
        )
    )
    assert task.fail_to_pass == [
        "tests/test_adapter.py::test_a",
        "tests/test_adapter.py::test_b",
    ]
    assert task.tags == ["alpha", "zebra"]


def test_test_patch_paths_derived_when_absent() -> None:
    """不填 test_patch_paths 也行，模型自己从 test_patch 算出来（C-74 第 1 条）。

    这份清单本来就"由 Validator 推导，不是人工填写"。
    """
    task = TaskDefinition.model_validate(make_task(test_patch_paths=[]))
    assert task.test_patch_paths == ["tests/test_adapter.py"]


# ══════════════════════════════════════════════════════════
# 防泄题：下发给 AI 的字段
# ══════════════════════════════════════════════════════════


def test_agent_visible_dump_hides_answers() -> None:
    """下发给被测 AI 的那份数据里，不能有答案，也不能有定位提示。

    - `gold_patch`：官方补丁，协议 C-44 明令禁止下发
    - `test_patch` / `test_patch_paths`：等于告诉 AI 官方测试改了哪几个文件（C-76）
    - `fail_to_pass` / `pass_to_pass`：用例 ID 也是定位提示

    把这条边界钉在题目模型上，而不是让每个 Agent 适配器各自决定一次——
    适配器有六七个，漏一个就泄题，而且不会有任何报错。
    """
    visible = TaskDefinition.model_validate(load_fixture()).agent_visible_dump()
    for forbidden in (
        "gold_patch",
        "test_patch",
        "test_patch_paths",
        "fail_to_pass",
        "pass_to_pass",
    ):
        assert forbidden not in visible, f"{forbidden} 不能下发给被测 AI"
    # 该给的还是要给
    assert visible["issue_body"]
    assert visible["base_commit"]
