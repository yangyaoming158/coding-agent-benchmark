"""Stage2 特征提取的单测（E6-T1，`06-judge-attribution.md` §12.2 第二步）。

重点盯两件容易出静默 bug 的事：

1. **取不到的维度必须标 unavailable，不能用零值糊。** `jaccard = 0.0` 的意思是
   "一个文件都没蒙对"，和"没法算"差着十万八千里 —— 混起来 E6-T2 会直接判 F2。
2. **报错文本比对前要抹掉每次都变的东西**（对象地址、临时目录、耗时），
   否则条条都判"变了"，F4 和 F1 就再也分不开。
"""

from __future__ import annotations

from app.attribution.features import (
    Stage2Features,
    log_errors,
    message_shift,
    patch_overlap,
    trajectory_stats,
)


def test_overlap_half_the_files_hit() -> None:
    overlap = patch_overlap(
        ["src/click/core.py", "src/click/types.py"],
        ["src/click/core.py", "src/click/parser.py"],
    )
    assert overlap.shared == ["src/click/core.py"]
    assert overlap.hit_any is True
    # 交集 1，并集 3
    assert overlap.jaccard == round(1 / 3, 4)


def test_overlap_nothing_in_common() -> None:
    """F2（找错文件）的直接判据就是 hit_any 为假。"""
    overlap = patch_overlap(["docs/readme.md"], ["src/click/core.py"])
    assert overlap.hit_any is False
    assert overlap.shared == []


def test_overlap_dedupes_and_sorts() -> None:
    overlap = patch_overlap(["b.py", "a.py", "a.py"], ["a.py"])
    assert overlap.agent_paths == ["a.py", "b.py"]


def test_changed_message_means_a_new_code_path() -> None:
    shift = message_shift(
        current={"t::a": "AssertionError: expected 3 handlers, got 2"},
        baseline={"t::a": "AttributeError: 'NoneType' object has no attribute 'add'"},
    )
    assert shift.compared == 1
    assert shift.changed == 1
    assert shift.unchanged == 0


def test_unchanged_message_means_the_patch_did_nothing() -> None:
    same = "AttributeError: 'NoneType' object has no attribute 'add'"
    shift = message_shift(current={"t::a": same}, baseline={"t::a": same})
    assert shift.unchanged == 1
    assert shift.changed == 0


def test_addresses_and_temp_dirs_are_not_changes() -> None:
    """每次跑都不一样的东西不抹掉，条条都会被判成"变了"。"""
    shift = message_shift(
        current={"t::a": "boom at <obj 0xdeadbeef> in /tmp/pytest-123/x took 1.25s"},
        baseline={"t::a": "boom at <obj 0xcafe0001> in /tmp/pytest-999/x took 3.50s"},
    )
    assert shift.unchanged == 1
    assert shift.changed == 0


def test_message_gone_counts_as_fixed() -> None:
    shift = message_shift(current={"t::a": None}, baseline={"t::a": "AssertionError"})
    assert shift.fixed == ["t::a"]
    assert shift.compared == 0


def test_syntax_error_means_it_never_ran() -> None:
    errors = log_errors("E   SyntaxError: invalid syntax\nERROR collecting tests/test_a.py")
    assert errors.counts["syntax"] == 1
    assert errors.counts["collection"] == 1
    assert errors.blocked_before_running is True


def test_assertion_failure_is_not_blocked() -> None:
    errors = log_errors("E   AssertionError: expected 3, got 2")
    assert errors.blocked_before_running is False
    assert errors.counts == {"assertion": 1}


def test_trajectory_tool_error_ratio() -> None:
    lines = [
        '{"type": "tool_call", "name": "edit_file", "ok": true}',
        '{"type": "tool_call", "name": "run_tests", "ok": false}',
        '{"type": "llm_usage", "input": 100, "output": 20}',
        '{"type": "message", "role": "assistant"}',
    ]
    stats = trajectory_stats(lines)
    assert stats.tool_calls == 2
    assert stats.tool_errors == 1
    assert stats.tool_error_ratio == 0.5
    assert stats.llm_calls == 1
    assert stats.messages == 1


def test_trajectory_missing_ok_is_not_an_error() -> None:
    """老轨迹没有 `ok` 字段。缺省判失败会凭空造出一堆 F8。"""
    stats = trajectory_stats(['{"type": "tool_call", "name": "edit_file"}'])
    assert stats.tool_calls == 1
    assert stats.tool_errors == 0


def test_trajectory_skips_bad_lines() -> None:
    """轨迹是诊断材料，一行写坏了不该让整次归因失败。"""
    stats = trajectory_stats(["这不是 JSON", "", '{"type": "tool_call", "ok": true}', "[1,2]"])
    assert stats.tool_calls == 1


def test_trajectory_tail_keeps_the_last_ten() -> None:
    lines = [f'{{"type": "tool_call", "name": "t{i}", "ok": true}}' for i in range(25)]
    stats = trajectory_stats(lines)
    assert len(stats.tail) == 10
    assert stats.tail[-1]["name"] == "t24"


def test_missing_dimension_goes_to_unavailable() -> None:
    features = Stage2Features(unavailable={"patch_overlap": "题目没有官方补丁"})
    payload = features.to_dict()
    assert payload == {"unavailable": {"patch_overlap": "题目没有官方补丁"}}
    assert "patch_overlap" not in payload, "取不到的维度不许出现零值"


def test_all_four_feature_groups_serialize() -> None:
    features = Stage2Features(
        patch_overlap=patch_overlap(["a.py"], ["a.py"]),
        message_shift=message_shift({"t::a": "x"}, {"t::a": "y"}),
        log_errors=log_errors("SyntaxError: bad"),
        trajectory=trajectory_stats(['{"type": "tool_call", "ok": true}']),
    )
    payload = features.to_dict()
    assert set(payload) == {"patch_overlap", "message_shift", "log_errors", "trajectory"}
    assert payload["patch_overlap"]["hit_any"] is True
