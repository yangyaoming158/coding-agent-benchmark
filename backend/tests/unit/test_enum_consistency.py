"""把代码里的枚举和常量表与协议原文逐条对照（协议 C-47）。

**为什么要有这个测试**：协议是冻结的，代码是会改的。没有这道检查，
某天有人给 `InfraOutcome` 加一个值、或者把某个故障的重试次数从 1 改成 2，
代码照样跑得通，只有到出报告的时候才会发现口径对不上，而那时候已经
跑完几百次评测了。

做法是直接解析 `docs/evaluation-protocol.md` 的 markdown，不维护第二份清单
—— 维护第二份清单本身就会漂移。
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

import pytest

from app.domain.enums import PROTOCOL_ENUMS
from app.domain.protocol import (
    INFRA_TO_AGENT_MAPPING,
    LEGAL_COMBINATIONS,
    PROTOCOL_VERSION,
    InfraFailureCounting,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROTOCOL_PATH = PROJECT_ROOT / "docs" / "evaluation-protocol.md"


@pytest.fixture(scope="module")
def protocol_text() -> str:
    return PROTOCOL_PATH.read_text(encoding="utf-8")


def _clause_section(text: str, clause: str) -> str:
    """截出一条条款的正文：从 `**C-XX【` 开始，到下一条条款、分隔线或标题为止。"""
    marker = f"**{clause}【"
    assert marker in text, f"协议里找不到条款 {clause}"
    rest = text[text.index(marker) + len(marker) :]
    ends = [rest.index(m) for m in ("**C-", "\n---\n", "\n## ") if m in rest]
    return rest[: min(ends)] if ends else rest


def _table_rows(section: str) -> list[list[str]]:
    """把 markdown 表格的数据行拆成单元格。表头行和 `|:---|` 分隔行会被跳过。"""
    rows: list[list[str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if all(set(c) <= set(":- ") for c in cells):
            continue
        rows.append(cells)
    return rows


def _table_after_clause(text: str, clause: str) -> list[list[str]]:
    """取一条条款后面的第一张表格。

    C-18 需要单独这么处理：协议里那张映射表排在 C-19 后面（C-19 是一句
    "禁止把这张表散落到 if 分支里"的补充规定），按条款边界截段会把表格切掉。
    """
    marker = f"**{clause}【"
    assert marker in text, f"协议里找不到条款 {clause}"
    rest = text[text.index(marker) :]
    lines = rest.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("|"))
    end = start
    while end < len(lines) and lines[end].strip().startswith("|"):
        end += 1
    return _table_rows("\n".join(lines[start:end]))


def _enum_values_in_clause(text: str, clause: str) -> list[str]:
    """取一条条款里列举的枚举取值。

    有两种写法要兼容：表格（第一列是取值），和一行反引号顿号串。
    表格必须只取第一列 —— C-08 的"判定条件"列里出现了 `PASSED`，
    整段抓会把它当成 agent_outcome 的一个取值。
    """
    section = _clause_section(text, clause)
    rows = _table_rows(section)
    if rows:
        # 跳过表头行（第一列不是反引号包起来的大写标识符）
        values = [m.group(1) for row in rows if (m := re.fullmatch(r"`([A-Z][A-Z0-9_]*)`", row[0]))]
        if values:
            return values
    return list(dict.fromkeys(re.findall(r"`([A-Z][A-Z0-9_]*)`", section)))


@pytest.mark.parametrize("clause", sorted(PROTOCOL_ENUMS))
def test_protocol_enum_matches_document(protocol_text: str, clause: str) -> None:
    """协议枚举的取值必须与协议原文逐字一致，顺序也要一致（协议 C-47）。

    顺序也检查，是因为原生枚举类型在数据库里是有序的（可以直接 ORDER BY），
    顺序变了会静默改变排序结果。
    """
    enum_cls: type[StrEnum] = PROTOCOL_ENUMS[clause]
    expected = _enum_values_in_clause(protocol_text, clause)
    actual = [member.value for member in enum_cls]
    assert actual == expected, (
        f"{enum_cls.__name__}（协议 {clause}）与协议原文不一致：\n"
        f"  代码里：{actual}\n"
        f"  协议里：{expected}"
    )


def test_infra_mapping_covers_every_infra_outcome(protocol_text: str) -> None:
    """C-18 的映射表必须覆盖 infra_outcome 的每一个取值，不多不少。"""
    expected = set(_enum_values_in_clause(protocol_text, "C-05"))
    actual = {outcome.value for outcome in INFRA_TO_AGENT_MAPPING}
    assert actual == expected


def test_infra_mapping_retry_and_failure_counting(protocol_text: str) -> None:
    """重试次数和"计不计入平台故障率"两列必须与 C-18 的表格一致。

    只对照这两列，不对照"责任方"和"映射到的 agent_outcome"：
    那两列在协议里是中文描述（"按 C-69 定"），翻译成枚举的规则本身
    就得写一份对照表，那份对照表又会成为新的漂移源。
    合法组合表（下一个测试）已经把映射结果盖住了。
    """
    counting_by_text = {
        "是": InfraFailureCounting.YES,
        "否": InfraFailureCounting.NO,
        "见 C-20": InfraFailureCounting.BY_CONTROL_RUN,
    }

    checked = 0
    for row in _table_after_clause(protocol_text, "C-18"):
        name_match = re.fullmatch(r"`([A-Z][A-Z0-9_]*)`", row[0])
        if not name_match:
            continue
        rule = INFRA_TO_AGENT_MAPPING[
            next(k for k in INFRA_TO_AGENT_MAPPING if k.value == name_match.group(1))
        ]

        counting_cell = row[3].strip("*")
        assert counting_cell in counting_by_text, f"C-18 表格里没见过的写法：{counting_cell!r}"
        assert rule.counts_as_infra_failure == counting_by_text[counting_cell], (
            f"{name_match.group(1)} 的平台故障率计入方式与协议不一致"
        )

        retry_cell = row[4].strip("*")
        if retry_cell == "—":
            expected_retries = 0
        elif retry_cell == "见 C-20":
            expected_retries = 1  # C-20 走完对照流程后仍按 1 次重试
        else:
            expected_retries = int(re.match(r"\d+", retry_cell).group())  # type: ignore[union-attr]
        assert rule.max_auto_retries == expected_retries, (
            f"{name_match.group(1)} 的自动重试次数与协议不一致："
            f"代码 {rule.max_auto_retries}，协议 {retry_cell}"
        )
        checked += 1

    assert checked == len(INFRA_TO_AGENT_MAPPING), "C-18 表格的行数和映射表对不上"


def test_legal_combinations_match_truth_table(protocol_text: str) -> None:
    """合法组合表必须与协议 §4.3 穷举出来的那张表逐行一致（协议 C-68、C-78）。

    §4.3 的表是 `docs/_protocol_truth_table.py` 生成的，所以这条检查
    等于同时锁住了脚本、协议正文和代码三方。
    """
    heading = "### 全部合法组合"
    assert heading in protocol_text, "协议 §4.3 的合法组合表不见了"
    section = protocol_text[protocol_text.index(heading) :]
    section = section[: section.index("\n**C-78")]

    expected: list[tuple[str, str, str | None, str]] = []
    for row in _table_rows(section):
        if not re.fullmatch(r"`[A-Z][A-Z0-9_]*`", row[0]):
            continue  # 表头，以及"全部非终态"那条通则
        agent_cell = row[2].strip("`")
        expected.append(
            (
                row[0].strip("`"),
                row[1].strip("`"),
                None if agent_cell == "NULL" else agent_cell,
                row[3].strip(),
            )
        )

    actual = [
        (
            combo.lifecycle_status.value,
            combo.infra_outcome.value,
            combo.agent_outcome.value if combo.agent_outcome else None,
            combo.condition,
        )
        for combo in LEGAL_COMBINATIONS
    ]
    assert actual == expected


def test_protocol_version_matches_document(protocol_text: str) -> None:
    """代码里写的协议版本号必须和协议头部的状态一致（协议 C-67）。"""
    match = re.search(r"\*\*状态\*\* \| \*\*(?:DRAFT|FROZEN) (v[\d.]+)", protocol_text)
    assert match is not None, "协议头部的状态表格格式变了"
    assert match.group(1) == PROTOCOL_VERSION
