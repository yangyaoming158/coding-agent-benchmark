"""评测协议的一致性检查，跑在 CI 里。

对应协议条款 C-79：任何影响状态取值的改动，都要重跑真值表脚本并同步输出。
把它放进 CI，是为了让"改了协议但忘了重跑检查"这件事直接让构建失败，
而不是等到几周后实现判定引擎时才发现两边对不上。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = REPO_ROOT / "docs" / "evaluation-protocol.md"
TRUTH_TABLE = REPO_ROOT / "docs" / "_protocol_truth_table.py"

# 匹配形如 **C-12【必须】 的条款定义
CLAUSE_DEF = re.compile(r"\*\*(C-\d+[a-z]?)【")
CLAUSE_REF = re.compile(r"C-\d+[a-z]?")


@pytest.fixture(scope="module")
def protocol_text() -> str:
    """协议全文。"""
    return PROTOCOL.read_text(encoding="utf-8")


def _clause_ids(text: str) -> list[str]:
    """按出现顺序返回所有被定义的条款编号（含重复）。"""
    return CLAUSE_DEF.findall(text)


def test_protocol_files_exist() -> None:
    """协议正文和真值表脚本都必须在。"""
    assert PROTOCOL.is_file(), f"找不到评测协议：{PROTOCOL}"
    assert TRUTH_TABLE.is_file(), f"找不到真值表脚本：{TRUTH_TABLE}"


def test_truth_table_has_no_gaps_or_overlaps() -> None:
    """穷举 lifecycle × infra_outcome × agent_outcome 的全部组合。

    脚本自身在发现空洞（某个取值到不了）或未区分的重叠时返回非 0。
    """
    result = subprocess.run(
        [sys.executable, str(TRUTH_TABLE)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, (
        f"真值表检查未通过，说明协议里出现了空洞或未区分的重叠：\n{result.stdout}\n{result.stderr}"
    )
    assert "无空洞、无未区分的重叠" in result.stdout


def test_protocol_section_matches_truth_table_output(protocol_text: str) -> None:
    """协议 §4.3 里贴的那张合法组合表，必须是脚本当前输出的那一份（协议 C-79）。

    没有这条检查的话会出现一种很难发现的情况：有人改了真值表脚本，
    脚本自己跑起来还是绿的（它只检查有没有空洞和重叠），但协议正文里
    贴的还是旧表。而代码是照着协议正文写的，于是脚本和代码悄悄分了家。
    """
    result = subprocess.run(
        [sys.executable, str(TRUTH_TABLE)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )
    generated = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().startswith("|") and "---" not in line
    ]

    heading = "### 全部合法组合"
    assert heading in protocol_text, "协议 §4.3 的合法组合表不见了"
    section = protocol_text[protocol_text.index(heading) : protocol_text.index("\n**C-78")]
    in_doc = [
        line.strip()
        for line in section.splitlines()
        if line.strip().startswith("|") and "---" not in line
    ]

    assert in_doc == generated, (
        "协议 §4.3 的表和真值表脚本的输出对不上。"
        "改完协议要重跑 `python3 docs/_protocol_truth_table.py` 并把输出贴回 §4.3。"
    )


def test_clause_ids_are_unique(protocol_text: str) -> None:
    """同一个编号不能定义两次，否则引用时不知道指哪条。"""
    ids = _clause_ids(protocol_text)
    duplicates = sorted({c for c in ids if ids.count(c) > 1})
    assert not duplicates, f"以下条款被定义了多次：{duplicates}"


def test_clause_ids_have_no_gaps(protocol_text: str) -> None:
    """编号必须连续。缺号通常意味着某条被误删了。"""
    numbers = sorted(
        {int(m.group(1)) for c in _clause_ids(protocol_text) if (m := re.match(r"C-(\d+)", c))}
    )
    gaps = [n for n in range(1, max(numbers) + 1) if n not in numbers]
    assert not gaps, f"以下编号被跳过了：{gaps}"


def test_no_dangling_clause_references(protocol_text: str) -> None:
    """防止改版时删掉了某条，但别处还在引用它。"""
    defined = set(_clause_ids(protocol_text))
    referenced = set(CLAUSE_REF.findall(protocol_text))
    dangling = sorted(referenced - defined)
    assert not dangling, f"这些条款被引用但没有定义：{dangling}"


def test_title_version_matches_status_table(protocol_text: str) -> None:
    """曾经出过一次：改了状态表格里的版本号，但 H1 标题还停在旧版本。"""
    title_ver = re.search(r"^# 评测协议 (v[\d.]+)", protocol_text, re.M)
    status_ver = re.search(r"\*\*状态\*\* \| \*\*(?:DRAFT|FROZEN) (v[\d.]+)", protocol_text)
    assert title_ver and status_ver, "标题或状态表格里找不到版本号"
    assert title_ver.group(1) == status_ver.group(1), (
        f"标题写的是 {title_ver.group(1)}，状态表格写的是 {status_ver.group(1)}，两者必须一致"
    )
