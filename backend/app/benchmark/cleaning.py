"""候选清洗：脱敏、拆补丁、抽候选 F2P（E1-T5，`03-benchmark-spec.md` §8.4）。

一句话：**把 E1-T4 抄下来的原始候选，加工成能拿去打分和建题的形状。**

这一层不联网、不调模型、不写库，全是纯函数。真正花钱的 LLM 预筛在
`app.benchmark.prescreen`，取 diff 和落库在 `cli/prescreen.py`。

## 三件事，各自的坑不一样

1. **脱敏**（`redact_issue`）——错了会**泄题**，那道题直接废掉。
2. **拆补丁**（`split_diff`）——错了 `git apply` 会报 corrupt patch，或者
   官方测试补丁把 bug 一起修掉，题目失去区分度。
3. **抽候选 F2P**（`extract_f2p_candidates`）——错了只是少几条候选，
   **验证流水线会兜住**（§7.2(5) 要求实测证伪），所以这一步宁可多给不少给。

## 脱敏剥什么、不剥什么

剥三样，每样都有明确形状：

- **本仓库的 PR / issue 链接**（AC 点名）—— 顺着链接就能看到官方补丁
- **40 位裸 commit hash**（AC 点名）—— 拿它能直接 checkout 出修复后的代码
- **`diff --git` 补丁块** —— 那就是答案本身

**普通代码块保留。** 因为 issue 里的代码块绝大多数是**复现代码和报错日志，不是修复
方案**，而贴复现代码恰恰是好 issue 的特征。一律剥掉会把题面剥废。"这段代码是不是
修复方案"没有正则形状，交给 LLM 预筛判（§7.2(7) 那张表里"Issue 里直接给了修复代码"
写的正是"正则 + LLM 预筛"，本来就是两道防线）。

顺带说明一处和 §7.9 的表面张力：导入校验器（`schema.py`）**故意不查裸 commit
hash**，因为用户贴报错日志时带哈希很常见，按那个拒收会误伤一大批好题。
这里却要剥掉它。两者方向一致，不冲突：**建题时脱敏，导入时不拒收。**
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.domain.patch_paths import DiffSection, iter_diff_sections
from app.domain.protected_paths import (
    DEFAULT_PROTECTED_PATTERNS,
    is_protected,
    normalize_path,
)

#: 清洗结果的形状版本。改了脱敏规则或拆分口径要跟着升，
#: 否则缓存和已落库的结果会和新代码悄悄混用。
CLEANER_VERSION = "1.0"

# ══════════════════════════════════════════════════════════════
# 脱敏
# ══════════════════════════════════════════════════════════════

#: 替换进去的占位符。用中文方括号而不是空字符串：
#: 直接删掉会让句子断掉（"修复见 " 后面突然没了），读的人和模型都会困惑；
#: 留一个标记则明确告诉它"这里本来有个东西，被拿掉了"。
PR_LINK_PLACEHOLDER = "［链接已移除］"
HASH_PLACEHOLDER = "［提交哈希已移除］"
DIFF_PLACEHOLDER = "［补丁已移除］"

#: 40 位十六进制 —— git 的完整 SHA-1。
#:
#: 用 `\b` 卡两头，而且**只认 40 位整**：短哈希（7~12 位）不剥，
#: 因为那个长度的十六进制串在报错日志里到处都是（内存地址、UUID 片段、
#: 十六进制转储），剥了会把题面打得千疮百孔。40 位几乎只可能是 commit id。
_FULL_HASH = re.compile(r"\b[0-9a-f]{40}\b")

#: 一整块 unified diff。从 `diff --git` 一直吃到下一个非补丁行为止。
#:
#: 连着后面的 `---/+++/@@/+/-/空格` 行一起吃：只删 `diff --git` 那一行的话，
#: 补丁正文还原样留在题面里，等于什么都没脱。
_DIFF_BLOCK = re.compile(
    r"^diff --git .*(?:\n(?:index |--- |\+\+\+ |@@ |[+\- ]|\\ No newline).*)*",
    re.MULTILINE,
)

#: 单独出现的 `--- a/x` + `+++ b/x` + `@@` 三件套（有人贴补丁不带 `diff --git`）。
_BARE_PATCH = re.compile(
    r"^--- (?:a/|/dev/null).*\n\+\+\+ .*\n(?:@@ .*(?:\n(?:[+\- ]|\\ No newline).*)*)+",
    re.MULTILINE,
)


def repo_link_pattern(repo: str) -> re.Pattern[str]:
    """匹配指向**本仓库**的 PR / issue / commit 链接。

    只剥本仓库的：题面里链到 CPython 上游 issue、链到某个依赖库的 PR，
    都是正常的背景信息，剥了反而损失题意。答案只可能在本仓库里。

    同时认 `#123` 这种裸引用吗？**不认。** 那是 issue 之间正常的互相引用，
    没有它读者常常看不懂来龙去脉；而且光有个号也翻不到补丁内容
    （被测 AI 在沙箱里访问不了 github.com，§7.6 的域名白名单挡着）。
    """
    owner_repo = re.escape(repo)
    return re.compile(
        rf"https?://(?:www\.)?github\.com/{owner_repo}/(?:pull|issues|commit)/\S+",
        re.IGNORECASE,
    )


@dataclass(frozen=True, slots=True)
class RedactionResult:
    """脱敏之后的正文，加上"删了什么"的账。

    记账是为了让报表能回答"这批题面被动了多少"—— 一条 issue 被剥掉五处链接，
    多半说明它本来就在讨论具体的修复过程，值得人看一眼。
    """

    text: str
    pr_links: int = 0
    hashes: int = 0
    diff_blocks: int = 0

    @property
    def total(self) -> int:
        return self.pr_links + self.hashes + self.diff_blocks

    def to_json(self) -> dict[str, Any]:
        return {
            "pr_links": self.pr_links,
            "hashes": self.hashes,
            "diff_blocks": self.diff_blocks,
            "total": self.total,
        }


def redact_issue(text: str, *, repo: str) -> RedactionResult:
    """把一段 issue 正文脱敏。顺序有讲究，见下。

    **先剥补丁块，再剥链接和哈希。** 反过来的话，补丁块里的
    `index 3f2a1c9e...b7cf0697` 会先被哈希规则换成占位符，
    于是补丁块的正则就对不上了，整块补丁留在题面里 —— 而且不报错。
    """
    diff_blocks = 0
    cleaned, count = _DIFF_BLOCK.subn(DIFF_PLACEHOLDER, text)
    diff_blocks += count
    cleaned, count = _BARE_PATCH.subn(DIFF_PLACEHOLDER, cleaned)
    diff_blocks += count

    cleaned, pr_links = repo_link_pattern(repo).subn(PR_LINK_PLACEHOLDER, cleaned)
    cleaned, hashes = _FULL_HASH.subn(HASH_PLACEHOLDER, cleaned)

    return RedactionResult(text=cleaned, pr_links=pr_links, hashes=hashes, diff_blocks=diff_blocks)


def leaks_remaining(text: str, *, repo: str) -> list[str]:
    """脱敏之后再查一遍，返回还剩下的泄题形式。空列表表示干净。

    这是 AC 那条"正则断言"的实现。**和 `redact_issue` 用同一套正则**：
    两套的话，脱敏漏掉的东西检查也照样漏掉，这个断言就成了摆设。
    """
    found = []
    if repo_link_pattern(repo).search(text):
        found.append("仓库 PR/issue 链接")
    if _FULL_HASH.search(text):
        found.append("40 位 commit hash")
    if _DIFF_BLOCK.search(text) or _BARE_PATCH.search(text):
        found.append("diff 补丁块")
    return found


# ══════════════════════════════════════════════════════════════
# 拆补丁
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class SplitPatches:
    """一个 PR 的 diff 拆成三份。"""

    #: 只碰受保护路径的段 —— 测试用例、conftest、pytest 配置。由 harness 施加。
    test_patch: str
    #: 一个受保护路径都不碰的段 —— 被测 AI 要写的东西，也就是 `gold_patch`。
    code_patch: str
    #: 一段里既有受保护路径又有普通路径（跨界改名/复制）。**两边都不放。**
    dropped: tuple[str, ...] = ()
    test_paths: tuple[str, ...] = ()
    code_paths: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        """两边都非空才立得起一道题（§7.2(7) 的两条结构规则）。"""
        return bool(self.test_patch.strip()) and bool(self.code_patch.strip())

    def to_json(self) -> dict[str, Any]:
        return {
            "test_paths": list(self.test_paths),
            "code_paths": list(self.code_paths),
            "dropped_paths": list(self.dropped),
            "test_patch_bytes": len(self.test_patch),
            "code_patch_bytes": len(self.code_patch),
            "usable": self.usable,
        }


def split_diff(diff: str, *, patterns: Sequence[str] = DEFAULT_PROTECTED_PATTERNS) -> SplitPatches:
    """按受保护路径把 PR 的 diff 劈成 `(test_patch, code_patch)`。

    ## 为什么按"段"劈，不按行

    一段就是 `diff --git` 到下一个 `diff --git` 之间的全部内容。删掉 hunk 里的
    几行之后，hunk 头 `@@ -a,b +c,d @@` 声明的行数就和实际对不上，
    `git apply` 会报 `corrupt patch`（`DiffSection` 的文档里写着这条）。

    ## 为什么口径和 `cli/golden.py` 完全一样

    用整份 `DEFAULT_PROTECTED_PATTERNS`，不是只用 `TEST_CODE_PATTERNS`。三个理由：

    1. **`conftest.py` 必须跟着测试走。** 一个 PR 常常同时加一条测试和它要用的
       fixture。conftest 落到 code_patch 里的话，打上 test_patch 之后新测试
       会因为 fixture 不存在而 ERROR —— 看起来像"这道题的 F2P 挂了"，其实是我们劈错了。
    2. **`schema.py` 就是这么校验的**：`test_patch` 的每条路径都必须命中
       `DEFAULT_PROTECTED_PATTERNS`，`gold_patch` 一条都不能命中。
       劈法和校验口径不一致的话，劈出来的东西根本导不进去。
    3. Golden 题（`cli/golden.py:split_patches`）也是这个口径。两套口径的话，
       "这里算测试、那里算源码"迟早出事。

    ## 跨界的那一段直接丢

    协议 C-62：重命名或复制时**新旧路径任一受保护就整个文件丢弃**。
    把 `src/x.py` 改名成 `tests/x.py` 的那一段，放 test_patch 会把源码改动
    塞进官方测试补丁，放 code_patch 又等于让被测 AI 改测试。两边都不对，所以丢掉
    并记下来 —— 记下来是为了让"这道题为什么劈不出来"在报表上看得见。
    """
    test_parts: list[str] = []
    code_parts: list[str] = []
    dropped: list[str] = []
    test_paths: set[str] = set()
    code_paths: set[str] = set()

    for section in iter_diff_sections(diff):
        paths = section.paths
        if not paths:
            continue
        protected = [p for p in paths if is_protected(p, tuple(patterns))]
        if len(protected) == len(paths):
            test_parts.append(section.text)
            test_paths.update(paths)
        elif not protected:
            code_parts.append(section.text)
            code_paths.update(paths)
        else:
            dropped.extend(paths)

    return SplitPatches(
        test_patch="".join(test_parts),
        code_patch="".join(code_parts),
        dropped=tuple(sorted(set(dropped))),
        test_paths=tuple(sorted(test_paths)),
        code_paths=tuple(sorted(code_paths)),
    )


# ══════════════════════════════════════════════════════════════
# 抽候选 F2P
# ══════════════════════════════════════════════════════════════

#: 一个测试函数的定义行。`async def` 也算 —— pytest-asyncio 的用例就长这样。
_TEST_DEF = re.compile(r"^(?P<indent>\s*)(?:async\s+)?def\s+(?P<name>test\w*)\s*\(")
#: 测试类。pytest 只收集 `Test` 开头且没有 `__init__` 的类。
_TEST_CLASS = re.compile(r"^(?P<indent>\s*)class\s+(?P<name>Test\w*)\s*[(:]")
#: hunk 头。git 会把**外层的函数或类**写在 `@@ ... @@` 后面，是免费的上下文。
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@\s*(?P<context>.*)$")


@dataclass
class F2PCandidates:
    """从 test_patch 抽出来的候选用例 ID。

    **是"候选"不是 `fail_to_pass`。** §7.2(5) 规定候选必须经实测证伪：
    `base + test_patch` 上必须 FAILED/ERROR，加上 gold 之后必须 PASSED，
    两条都满足才算数。那是 E1-T3 验证流水线的事，这一步给不出结论。

    字段名和文案都写死"候选"就是为了这个 —— 直接叫 `fail_to_pass` 的话，
    下一个人很容易当成定论用，而错了不会报错，只会让解决率莫名其妙偏低。
    """

    ids: list[str] = field(default_factory=list)
    #: 新增了行、但归不到任何测试函数名下的行数。太多说明抽漏了，值得人看一眼。
    unattributed_lines: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "f2p_candidates": list(self.ids),
            "count": len(self.ids),
            "unattributed_lines": self.unattributed_lines,
        }


def _class_from_context(context: str) -> str | None:
    """从 hunk 头后面那截上下文里认出测试类名。

    git 在 `@@ -a,b +c,d @@` 后面会附上**外层的函数或类**，白送的上下文。
    hunk 正文里看不到 `class TestFoo:` 那一行时（改的是类中间的某个方法），
    全靠它才知道这条测试属于哪个类 —— 少了类名，用例 ID 就对不上。
    """
    hit = _TEST_CLASS.match(context)
    return hit.group("name") if hit else None


@dataclass
class _Walker:
    """边走一段 diff 边记"现在在哪个类、哪个测试函数里"。

    单独拎成一个类而不是在循环里套闭包：闭包会捕获循环变量，
    ruff 的 B023 会拦（而且它拦得对 —— 那种写法一旦挪个位置就静默取到旧值）。
    """

    path: str
    seen: set[str]
    out: list[str]

    hunk_class: str | None = None
    current_class: str | None = None
    current_test: str | None = None
    current_indent: int = 0
    pending_added: bool = False

    def flush(self) -> None:
        """当前这个测试函数如果有新增行，就记成一条候选。"""
        if self.pending_added and self.current_test:
            parts = [self.path, self.current_class, self.current_test]
            candidate = "::".join(p for p in parts if p)
            if candidate not in self.seen:
                self.seen.add(candidate)
                self.out.append(candidate)
        self.pending_added = False

    def on_hunk(self, context: str) -> None:
        self.flush()
        # 新 hunk：函数上下文断了，但 git 在 `@@ ... @@` 后面给了外层名字
        self.hunk_class = _class_from_context(context) or self.hunk_class
        self.current_class = self.hunk_class
        self.current_test = None

    def on_class(self, name: str) -> None:
        self.flush()
        self.current_class = name
        self.current_test = None

    def on_test_def(self, name: str, indent: int, *, added: bool) -> None:
        self.flush()
        self.current_test = name
        self.current_indent = indent
        # 缩进为 0 说明是模块级函数，不在任何类里
        if indent == 0:
            self.current_class = None
        self.pending_added = added

    def on_body(self, line: str, *, added: bool) -> bool:
        """函数体里的一行。返回 True 表示这行没归到任何测试函数名下。

        **空行即使是新增的也不算"这条测试被改了"。** 两条测试之间那个空行属于
        上一条测试的尾巴，把它算进去的话，"在 A 后面新加一条 B"会连 A 一起
        报成候选 —— 而 A 一个字都没动，它在基线上本来就是通过的，
        当成 F2P 会让验证流水线判 `F2P_NOT_FAILING`，整道好题被丢掉。
        """
        stripped = line.strip()
        if self.current_test is None:
            return added and bool(stripped)
        # 回到更外层的缩进（空行不算，它没有缩进信息）说明这个测试函数结束了
        if stripped and len(line) - len(line.lstrip()) <= self.current_indent:
            self.flush()
            self.current_test = None
            return added
        if added and stripped:
            self.pending_added = True
        return False


def extract_f2p_candidates(test_patch: str) -> F2PCandidates:
    """从 test_patch 里抽出被新增或改动的测试用例 ID（§7.2(5) 的"候选来源"）。

    ## 怎么抽

    对每个 hunk 重建**改完之后的样子**（上下文行 + 新增行），边走边记
    "现在在哪个类、哪个测试函数里"，只要这个函数名下有任何一行是新增的，
    就把它记成候选。

    这样能同时抓到两种：**整条新加的测试**（def 行本身是新增的），
    和**在已有测试里加了断言**（def 行是上下文行）。只看新增行里的 `def test_`
    会把第二种整批漏掉，而那一种恰恰是最常见的 bugfix 形状。

    ## 参数化用例只给基名

    `@pytest.mark.parametrize` 展开之后的真实 ID 带方括号
    （`test_x[case1]`），而那要跑一遍才知道有几个。这里只给 `test_x`，
    靠 E4 的用例 ID 归一化去对（`05-sandbox.md` 那条坑，AGENTS.md §5.5）。

    ## 抽多了不要紧，抽少了要紧

    抽出来的只是候选，验证流水线会逐条证伪：base 上没挂的会被剔掉。
    所以这里的取舍一律偏向"宁可多给"。
    """
    result = F2PCandidates()
    seen: set[str] = set()

    for section in iter_diff_sections(test_patch):
        paths = [p for p in section.paths if p]
        if not paths:
            continue
        # 一段里可能有旧路径和新路径（改名），用最后一个 —— 那是改完之后的名字
        walker = _Walker(path=normalize_path(paths[-1]), seen=seen, out=result.ids)

        for raw in section.text.splitlines():
            header = _HUNK_HEADER.match(raw)
            if header:
                walker.on_hunk(header.group("context"))
                continue
            if not raw or raw[0] not in "+ -" or raw.startswith("-"):
                # 删掉的行不构成"改完之后的样子"；文件头那几行也不是内容
                continue
            added = raw.startswith("+")
            line = raw[1:]

            klass = _TEST_CLASS.match(line)
            if klass:
                walker.on_class(klass.group("name"))
                continue

            test = _TEST_DEF.match(line)
            if test:
                walker.on_test_def(test.group("name"), len(test.group("indent")), added=added)
                continue

            if walker.on_body(line, added=added):
                result.unattributed_lines += 1

        walker.flush()

    return result


def issue_text(issues: Iterable[dict[str, Any]]) -> tuple[str, str]:
    """关联 issue 列表 → `(标题, 正文)`。

    多个关联 issue 时把正文按顺序拼起来，标题取第一个：题面要自足，
    只取一个 issue 会漏掉另一半上下文；而标题拼起来读着很怪。
    """
    items = [i for i in issues if i]
    if not items:
        return "", ""
    title = str(items[0].get("title") or "")
    bodies = [str(i.get("body") or "") for i in items]
    return title, "\n\n---\n\n".join(b for b in bodies if b.strip())


__all__ = [
    "CLEANER_VERSION",
    "DIFF_PLACEHOLDER",
    "HASH_PLACEHOLDER",
    "PR_LINK_PLACEHOLDER",
    "DiffSection",
    "F2PCandidates",
    "RedactionResult",
    "SplitPatches",
    "extract_f2p_candidates",
    "issue_text",
    "leaks_remaining",
    "redact_issue",
    "repo_link_pattern",
    "split_diff",
]
