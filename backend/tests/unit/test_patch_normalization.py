"""补丁归一化的测试（E3-T3，`06-judge-attribution.md` §11.4）。

这一组不碰磁盘。真工作区上的捕获和 `git apply --3way` 在
`tests/sandbox/test_patch_capture.py`。

**最要紧的一条**：被丢掉的段和被留下的段不能互相牵连。丢错一段是把 AI 的修复
悄悄删了（判成"没修好"），留错一段是让作弊生效（解决率虚高）。两种都不报错。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.domain.protected_paths import agent_visible_patterns, enforcement_patterns
from app.runner.adapters import MockBehavior, MockRunner
from app.runner.patch import (
    MAX_FILE_BYTES,
    FilteredChange,
    FilterReason,
    is_noise_path,
    normalize_patch,
    patch_stats,
    write_patch,
)
from app.runner.protocol import AgentConfig
from tests.contract.runner_contract import make_task_input

SOURCE = (
    "diff --git a/src/app.py b/src/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def login(user, password):\n"
    "-    return True\n"
    "+    return bool(password)\n"
)

CHEAT_TEST = (
    "diff --git a/tests/test_login.py b/tests/test_login.py\n"
    "index 4444444..5555555 100644\n"
    "--- a/tests/test_login.py\n"
    "+++ b/tests/test_login.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def test_empty_password_is_rejected():\n"
    "-    assert not login(u, '')\n"
    "+    assert True\n"
)

NOISE = (
    "diff --git a/src/__pycache__/app.cpython-311.pyc b/src/__pycache__/app.cpython-311.pyc\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/__pycache__/app.cpython-311.pyc\n"
    "@@ -0,0 +1 @@\n"
    "+garbage\n"
)

AIDER_NOISE = (
    "diff --git a/.aider.chat.history.md b/.aider.chat.history.md\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/.aider.chat.history.md\n"
    "@@ -0,0 +1 @@\n"
    "+聊天记录\n"
)

BINARY = (
    "diff --git a/assets/logo.png b/assets/logo.png\n"
    "index 6772730..3fa429c 100644\n"
    "Binary files a/assets/logo.png and b/assets/logo.png differ\n"
)

MODE_ONLY = "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n"

EMPTY_SECTION = "diff --git a/src/nothing.py b/src/nothing.py\nindex 111..222 100644\n"

ENFORCEMENT = enforcement_patterns()


# ── AC ①：改测试文件的那一段被剔除 ──────────────────────────


def test_protected_section_is_dropped_and_the_source_change_survives() -> None:
    """任务卡第一条验收：AI 改测试文件时该改动被剔除。

    另一半同样重要 —— 源码那一段必须**逐字节**留下来。连坐丢掉的话，
    AI 的真实修复被平台删了，最后判成"没修好"，而且没有任何报错。
    """
    result = normalize_patch(SOURCE + CHEAT_TEST, protected_patterns=ENFORCEMENT)

    assert result.text == SOURCE, "留下来的段必须逐字节不变"
    assert result.protected_path_edit_attempted
    assert [item.reason for item in result.filtered] == [FilterReason.PROTECTED_PATH]
    assert result.filtered[0].path == "tests/test_login.py"


def test_only_editing_protected_files_yields_an_empty_patch_with_evidence() -> None:
    """只改测试文件 → 标准化后为空，但原始补丁不空（协议 C-08a、C-08b）。

    这正是 `EMPTY_PATCH` 最容易被理解错的地方：它的含义是"标准化之后为空"，
    不等于"AI 什么都没做"。两者靠下面两个字段区分。
    """
    result = normalize_patch(CHEAT_TEST, protected_patterns=ENFORCEMENT)

    assert result.is_empty
    assert not result.raw_patch_empty, "原始补丁非空 —— AI 是动了手的，只是动错了地方"
    assert result.protected_path_edit_attempted


def test_doing_nothing_at_all_looks_different_from_only_cheating() -> None:
    """真的什么都没做：两个诊断字段都是 false。和上一条对照着看。"""
    result = normalize_patch("", protected_patterns=ENFORCEMENT)

    assert result.is_empty
    assert result.raw_patch_empty
    assert not result.protected_path_edit_attempted
    assert result.filtered == ()


def test_task_specific_test_paths_are_protected_too() -> None:
    """执行清单要含该题的 `test_patch_paths`（C-42 最后一条、C-74）。

    有些题的测试改动会带上名字完全不像测试的 fixture 文件，靠通配符匹配不到。
    这条证明"传了该题清单"和"没传"会得到不同结果 —— 也就是模块文档里说的，
    忘了传会让解决率静悄悄偏高。
    """
    fixture = (
        "diff --git a/data/reconnect.json b/data/reconnect.json\n"
        "--- a/data/reconnect.json\n"
        "+++ b/data/reconnect.json\n"
        "@@ -1 +1 @@\n"
        '-{"retries": 3}\n'
        '+{"retries": 0}\n'
    )
    generic = normalize_patch(fixture, protected_patterns=enforcement_patterns())
    with_task = normalize_patch(
        fixture, protected_patterns=enforcement_patterns(("data/reconnect.json",))
    )

    assert not generic.is_empty, "只有通用规则时这个文件不受保护"
    assert with_task.is_empty and with_task.protected_path_edit_attempted


def test_renaming_a_test_file_does_not_escape_the_filter() -> None:
    """把 `tests/test_a.py` 改名成 `helper.py` 再改内容 —— 整段丢掉（协议 C-62）。

    只看新路径的话这一招就成了：`helper.py` 不受保护。
    """
    rename = (
        "diff --git a/tests/test_a.py b/helper.py\n"
        "similarity index 80%\n"
        "rename from tests/test_a.py\n"
        "rename to helper.py\n"
        "--- a/tests/test_a.py\n"
        "+++ b/helper.py\n"
        "@@ -1 +1 @@\n"
        "-assert False\n"
        "+assert True\n"
    )
    result = normalize_patch(rename, protected_patterns=ENFORCEMENT)

    assert result.is_empty
    assert result.protected_path_edit_attempted


# ── AC ②：__pycache__ / .aider* 被忽略 ──────────────────────


def test_noise_files_are_dropped() -> None:
    """任务卡第二条验收。"""
    result = normalize_patch(SOURCE + NOISE + AIDER_NOISE, protected_patterns=ENFORCEMENT)

    assert result.text == SOURCE
    assert {item.reason for item in result.filtered} == {FilterReason.NOISE}
    assert not result.protected_path_edit_attempted, "噪声不是作弊，不该触发人工复核"


@pytest.mark.parametrize(
    "path",
    [
        "__pycache__/app.pyc",
        "src/__pycache__/app.cpython-311.pyc",
        "app.pyc",
        ".pytest_cache/v/cache/lastfailed",
        ".aider.chat.history.md",
        ".aider.tags.cache.v3/some-file",
        "node_modules/lib/index.js",
        ".venv/lib/python3.12/site.py",
        "debug.log",
        "src/app.py.orig",
        ".DS_Store",
    ],
)
def test_is_noise_path_recognizes_machine_generated_files(path: str) -> None:
    assert is_noise_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "src/app.py",
        "src/cache/store.py",
        "tests/test_app.py",
        "logs/reader.py",
        "build_tools/gen.py",
    ],
)
def test_is_noise_path_leaves_real_source_alone(path: str) -> None:
    """错挡一个真源文件，等于把 AI 的修复删了 —— 宁可漏挡也不能错挡。"""
    assert not is_noise_path(path)


def test_modifying_a_tracked_noise_looking_file_is_not_noise() -> None:
    """仓库**本来就跟踪着**一个 `debug.log`，Agent 改它是合法修复，不能当噪声丢。

    这和工作区那边是同一套语义：`.git/info/exclude` 只管物化之后新出现的文件，
    base 提交用的是 `git add -A --force`，跟踪中的文件即使命中忽略规则也照样提交。
    两边规则不一致的话，会出现"工作区里留着、补丁里被丢掉"的静默丢失。
    """
    modify_tracked_log = (
        "diff --git a/debug.log b/debug.log\n"
        "index 111..222 100644\n"
        "--- a/debug.log\n"
        "+++ b/debug.log\n"
        "@@ -1 +1 @@\n"
        "-level=info\n"
        "+level=debug\n"
    )
    result = normalize_patch(modify_tracked_log, protected_patterns=ENFORCEMENT)

    assert result.text == modify_tracked_log, "改的是跟踪中的文件，整段要留下"
    assert result.filtered == ()


def test_newly_created_noise_file_is_still_dropped() -> None:
    """反过来，新建的同名文件仍然是碎屑。和上一条一起把边界钉死。"""
    new_log = (
        "diff --git a/debug.log b/debug.log\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/debug.log\n"
        "@@ -0,0 +1 @@\n"
        "+level=debug\n"
    )
    result = normalize_patch(new_log, protected_patterns=ENFORCEMENT)

    assert result.is_empty
    assert [item.reason for item in result.filtered] == [FilterReason.NOISE]


def test_write_patch_always_ends_with_a_newline() -> None:
    """补丁最后一行没换行时 `git apply` 会报 corrupt patch，手工拼补丁时真会踩到。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "sub" / "p.patch"
        assert write_patch("diff --git a/x b/x", dest).read_text(encoding="utf-8").endswith("\n")
        assert write_patch("", dest).read_text(encoding="utf-8") == "", "空补丁写空文件"


# ── 其余几种丢弃原因 ────────────────────────────────────────


def test_binary_sections_are_dropped() -> None:
    result = normalize_patch(SOURCE + BINARY, protected_patterns=ENFORCEMENT)
    assert result.text == SOURCE
    assert [item.reason for item in result.filtered] == [FilterReason.BINARY]


def test_oversized_sections_are_dropped() -> None:
    body = "".join(f"+line {i}\n" for i in range(40_000))
    huge = (
        "diff --git a/src/generated.py b/src/generated.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/src/generated.py\n"
        f"@@ -0,0 +1,40000 @@\n{body}"
    )
    assert len(huge.encode()) > MAX_FILE_BYTES

    result = normalize_patch(SOURCE + huge, protected_patterns=ENFORCEMENT)
    assert result.text == SOURCE
    assert [item.reason for item in result.filtered] == [FilterReason.OVERSIZED]


def test_mode_only_and_empty_sections_are_dropped() -> None:
    result = normalize_patch(SOURCE + MODE_ONLY + EMPTY_SECTION, protected_patterns=ENFORCEMENT)

    assert result.text == SOURCE
    assert {item.reason for item in result.filtered} == {
        FilterReason.MODE_ONLY,
        FilterReason.EMPTY_SECTION,
    }


def test_protected_wins_over_binary_so_the_evidence_is_not_hidden() -> None:
    """`tests/` 下的二进制文件要报成 `protected_path`，不是 `binary`。

    报成 binary 就把"AI 动了测试目录"这条证据盖掉了，而那是要触发人工复核的
    信号（协议 C-13d）。分类顺序在 `_classify()` 里写死。
    """
    protected_binary = (
        "diff --git a/tests/fixtures/blob.bin b/tests/fixtures/blob.bin\n"
        "index 111..222 100644\n"
        "Binary files a/tests/fixtures/blob.bin and b/tests/fixtures/blob.bin differ\n"
    )
    result = normalize_patch(protected_binary, protected_patterns=ENFORCEMENT)

    assert [item.reason for item in result.filtered] == [FilterReason.PROTECTED_PATH]
    assert result.protected_path_edit_attempted


# ── 统计与落库形状 ──────────────────────────────────────────


def test_stats_count_files_and_lines() -> None:
    stats = patch_stats(SOURCE + CHEAT_TEST)
    assert stats.files_changed == 2
    assert (stats.lines_added, stats.lines_deleted) == (2, 2)
    assert stats.size_bytes == len((SOURCE + CHEAT_TEST).encode())
    assert not stats.is_empty


def test_empty_patch_stats_are_all_zero() -> None:
    stats = patch_stats("")
    assert (stats.files_changed, stats.lines_added, stats.lines_deleted) == (0, 0, 0)
    assert stats.is_empty


def test_stats_are_kept_for_both_the_raw_and_the_normalized_patch() -> None:
    """两份都要存。只存标准化的，"AI 试图改测试文件"就再也查不到了。"""
    result = normalize_patch(SOURCE + CHEAT_TEST, protected_patterns=ENFORCEMENT)

    assert result.raw_stats.files_changed == 2
    assert result.stats.files_changed == 1
    assert result.raw_stats.sha256 != result.stats.sha256


def test_filtered_change_reasons_serialize_for_the_jsonb_column() -> None:
    """`evaluation_task_runs.filtered_change_reasons` 是个 JSONB 列，
    写进去的每条记录必须是纯字符串字典。"""
    result = normalize_patch(SOURCE + CHEAT_TEST + BINARY, protected_patterns=ENFORCEMENT)
    records = result.filtered_change_reasons()

    assert len(records) == 2
    for record in records:
        assert set(record) == {"path", "reason", "detail"}
        assert all(isinstance(value, str) for value in record.values())
    assert {record["reason"] for record in records} == {"protected_path", "binary"}


def test_filtered_records_are_sorted_for_a_stable_diff() -> None:
    """同一个补丁跑两遍，记录顺序必须一样，否则落库的 JSON 每次都不同。"""
    first = normalize_patch(BINARY + CHEAT_TEST + MODE_ONLY, protected_patterns=ENFORCEMENT)
    second = normalize_patch(MODE_ONLY + CHEAT_TEST + BINARY, protected_patterns=ENFORCEMENT)
    assert first.filtered_change_reasons() == second.filtered_change_reasons()


def test_normalization_is_deterministic() -> None:
    """同一份输入两次归一化，结果逐字节相同 —— 补丁哈希要能当标识用。"""
    raw = SOURCE + CHEAT_TEST + NOISE + BINARY
    first = normalize_patch(raw, protected_patterns=ENFORCEMENT)
    second = normalize_patch(raw, protected_patterns=ENFORCEMENT)
    assert first.text == second.text
    assert first.stats == second.stats


# ── 和 E3-T2 的 Mock 对接 ───────────────────────────────────


def test_mock_protected_path_edit_is_split_the_way_it_should_be(tmp_path: Path) -> None:
    """MockRunner 的"改受保护文件"行为交出来的补丁，正好该被切成两半。

    这一条把 E3-T2 和 E3-T3 串起来了：适配器**保留**证据（契约第 4 条），
    平台在这里**剔除**它（协议 C-41）。两边方向相反是设计，不是矛盾。
    """
    task = make_task_input(deadline_ms=int(time.time() * 1000) + 60_000)
    raw = MockRunner(MockBehavior.PROTECTED_PATH_EDIT).run(task, tmp_path, AgentConfig()).patch
    result = normalize_patch(raw, protected_patterns=ENFORCEMENT)

    assert result.protected_path_edit_attempted
    assert not result.is_empty, "源码那一半要留下来"
    assert result.stats.files_changed == 1


def test_agent_visible_patterns_are_a_subset_of_what_we_enforce() -> None:
    """下发给 AI 的清单只是执行清单的一部分。

    反过来的话，就有 AI 看得见、平台却不拦的路径 —— 那等于告诉它"这里可以改"，
    然后又不去管它。
    """
    assert set(agent_visible_patterns()) <= set(ENFORCEMENT)


def test_filtered_change_record_shape() -> None:
    record = FilteredChange("tests/test_a.py", FilterReason.PROTECTED_PATH, "命中 tests/**")
    assert record.to_record() == {
        "path": "tests/test_a.py",
        "reason": "protected_path",
        "detail": "命中 tests/**",
    }
