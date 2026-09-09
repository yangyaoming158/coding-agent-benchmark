"""候选清洗：脱敏、拆补丁、抽候选 F2P（E1-T5，`03-benchmark-spec.md` §8.4）。

**这一组不联网、不调模型。** 三件事的错法代价差得很远，测的密度也照着来：

1. **脱敏错了会泄题**，那道题直接废掉，而且是"看起来正常、AI 却轻松满分"的废法。
   所以既测"该剥的剥掉了"，也测"不该剥的留着了"—— 后者一样重要，
   把复现代码剥光会把题面剥废。
2. **拆补丁错了** `git apply` 会报 corrupt patch，或者官方测试补丁把 bug 一起修掉。
3. **抽候选 F2P 抽漏了**只是少几条候选，验证流水线会兜住（§7.2(5) 要求实测证伪），
   所以这里的用例偏向"常见形状都能抓到"，不追求完备。
"""

from __future__ import annotations

from app.benchmark.cleaning import (
    DIFF_PLACEHOLDER,
    HASH_PLACEHOLDER,
    PR_LINK_PLACEHOLDER,
    extract_f2p_candidates,
    issue_text,
    leaks_remaining,
    redact_issue,
    split_diff,
)

REPO = "pallets/click"
FULL_HASH = "2d610e36a429bfebf0adb0ca90cdc0585f296369"

# ══════════════════════════════════════════════════════════════
# 脱敏：该剥的
# ══════════════════════════════════════════════════════════════


def test_repo_pr_link_is_removed() -> None:
    """顺着这个链接就能看到官方补丁。AC 点名要剥的第一样。"""
    result = redact_issue(f"修复见 https://github.com/{REPO}/pull/2946 那个 PR", repo=REPO)
    assert "pull/2946" not in result.text
    assert PR_LINK_PLACEHOLDER in result.text
    assert result.pr_links == 1


def test_repo_issue_and_commit_links_are_removed_too() -> None:
    text = (
        f"参考 https://github.com/{REPO}/issues/2945 和 "
        f"https://github.com/{REPO}/commit/{FULL_HASH}"
    )
    result = redact_issue(text, repo=REPO)
    assert "github.com" not in result.text
    assert result.pr_links == 2


def test_full_commit_hash_is_removed() -> None:
    """拿 40 位哈希能直接 checkout 出修复后的代码。AC 点名要剥的第二样。"""
    result = redact_issue(f"这个问题在 {FULL_HASH} 之后出现", repo=REPO)
    assert FULL_HASH not in result.text
    assert HASH_PLACEHOLDER in result.text
    assert result.hashes == 1


def test_diff_block_is_removed_whole() -> None:
    """补丁块就是答案本身。**要整块剥掉**，只删 `diff --git` 那一行等于没脱。"""
    text = (
        "我觉得应该这么改：\n\n"
        "diff --git a/src/click/types.py b/src/click/types.py\n"
        "index 3f2a1c9e..b7cf0697 100644\n"
        "--- a/src/click/types.py\n"
        "+++ b/src/click/types.py\n"
        "@@ -10,7 +10,7 @@ def convert(self):\n"
        "-        return value\n"
        "+        return value.strip()\n"
        "\n"
        "这样就好了。"
    )
    result = redact_issue(text, repo=REPO)
    assert "return value.strip()" not in result.text, "补丁正文必须一起剥掉"
    assert "diff --git" not in result.text
    assert DIFF_PLACEHOLDER in result.text
    assert "这样就好了" in result.text, "补丁前后的正文要留着"


def test_bare_patch_without_diff_git_header_is_removed() -> None:
    """有人贴补丁不带 `diff --git`，只有 `--- / +++ / @@` 三件套。"""
    text = "补丁：\n--- a/click/types.py\n+++ b/click/types.py\n@@ -1,3 +1,3 @@\n-old\n+new\n"
    result = redact_issue(text, repo=REPO)
    assert "+new" not in result.text
    assert result.diff_blocks == 1


def test_diff_is_stripped_before_hashes() -> None:
    """顺序错了会静默漏掉整块补丁。

    补丁块里有 `index 3f2a1c9e..b7cf0697`。先剥哈希的话那一行被改写，
    补丁块的正则就对不上了，整块补丁原样留在题面里 —— **而且不报错**。
    """
    text = (
        f"diff --git a/x.py b/x.py\nindex {FULL_HASH}..{'b' * 40} 100644\n"
        "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    result = redact_issue(text, repo=REPO)
    assert result.diff_blocks == 1
    assert "+b" not in result.text


# ══════════════════════════════════════════════════════════════
# 脱敏：不该剥的
# ══════════════════════════════════════════════════════════════


def test_reproduction_code_block_is_kept() -> None:
    """issue 里的代码块**绝大多数是复现代码，不是修复方案**。

    一律剥掉会把题面剥废 —— 而"贴了复现代码"恰恰是好 issue 的特征。
    "这段代码是不是修复方案"没有正则形状，交给 LLM 预筛判（§7.2(7) 写的是
    "正则 + LLM 预筛"，本来就是两道防线）。
    """
    text = "复现：\n\n```python\nfrom click import Path\nPath('x').convert('y')\n```\n\n报错如上。"
    result = redact_issue(text, repo=REPO)
    assert "Path('x').convert('y')" in result.text
    assert result.total == 0


def test_traceback_is_kept() -> None:
    """贴 traceback 是好 issue 的标志，一个字都不该动。"""
    text = 'Traceback (most recent call last):\n  File "<stdin>", line 1\nValueError: nope'
    assert redact_issue(text, repo=REPO).text == text


def test_short_hashes_are_kept() -> None:
    """7~12 位的十六进制在日志里到处都是（内存地址、UUID 片段）。

    按那个剥会把题面打得千疮百孔，而短哈希也 checkout 不出确定的东西。
    """
    text = "at 0x7f3a1c9e and commit 3f2a1c9 and id ab12cd34ef"
    assert redact_issue(text, repo=REPO).hashes == 0


def test_links_to_other_repos_are_kept() -> None:
    """链到上游 CPython、链到依赖库都是正常的背景信息，剥了反而损失题意。

    答案只可能在本仓库里。
    """
    text = "和 https://github.com/python/cpython/issues/12345 是同一个问题"
    result = redact_issue(text, repo=REPO)
    assert "cpython/issues/12345" in result.text
    assert result.pr_links == 0


def test_bare_issue_reference_is_kept() -> None:
    """`#123` 是 issue 之间正常的互相引用，没有它常常看不懂来龙去脉。

    而且光有个号也翻不到补丁 —— 被测 AI 在沙箱里访问不了 github.com（§7.6）。
    """
    assert "#2945" in redact_issue("和 #2945 相关", repo=REPO).text


# ══════════════════════════════════════════════════════════════
# 脱敏之后的自查（AC 那条正则断言）
# ══════════════════════════════════════════════════════════════


def test_leaks_remaining_is_empty_after_redaction() -> None:
    """AC：脱敏后 Issue 中不含仓库 PR 链接与 40 位 hash。"""
    text = (
        f"见 https://github.com/{REPO}/pull/2946，提交是 {FULL_HASH}\n\n"
        "diff --git a/x.py b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    assert leaks_remaining(text, repo=REPO), "脱敏之前应该查得出来"
    assert leaks_remaining(redact_issue(text, repo=REPO).text, repo=REPO) == []


def test_leaks_remaining_names_what_is_left() -> None:
    found = leaks_remaining(f"见 https://github.com/{REPO}/pull/1", repo=REPO)
    assert found == ["仓库 PR/issue 链接"]


# ══════════════════════════════════════════════════════════════
# 拆补丁
# ══════════════════════════════════════════════════════════════

DIFF = """\
diff --git a/src/click/types.py b/src/click/types.py
index 1111111..2222222 100644
--- a/src/click/types.py
+++ b/src/click/types.py
@@ -10,2 +10,2 @@ class Path:
     def convert(self):
-        return value
+        return value.strip()
diff --git a/tests/test_types.py b/tests/test_types.py
index 3333333..4444444 100644
--- a/tests/test_types.py
+++ b/tests/test_types.py
@@ -1,1 +1,5 @@
 import click
+
+
+def test_path_strips():
+    assert click.Path().convert(" x ") == "x"
"""


def test_split_puts_tests_and_source_on_opposite_sides() -> None:
    split = split_diff(DIFF)
    assert split.test_paths == ("tests/test_types.py",)
    assert split.code_paths == ("src/click/types.py",)
    assert "def test_path_strips" in split.test_patch
    assert "def test_path_strips" not in split.code_patch
    assert "return value.strip()" in split.code_patch
    assert split.usable is True


def test_each_section_stays_whole() -> None:
    """按"段"取舍，不按行。

    删掉 hunk 里的几行之后，hunk 头 `@@ -a,b +c,d @@` 声明的行数就和实际对不上，
    `git apply` 会报 corrupt patch。

    这条用例第一次写的时候，**夹具自己的 hunk 头就写错了**（声明 3 行只给了 2 行），
    于是解析器一路往下吃，把下一段的 `diff --git` 也吞了进去。那是解析器的
    **正确行为** —— git 生成的 diff 不会这样。所以本文件所有夹具的 `@@` 行数
    都是数准的，别随手改。
    """
    split = split_diff(DIFF)
    for patch in (split.test_patch, split.code_patch):
        assert patch.count("diff --git") == 1
        assert "@@ " in patch
        assert patch.startswith("diff --git")


def test_conftest_goes_with_the_tests() -> None:
    """一个 PR 常常同时加一条测试和它要用的 fixture。

    conftest 落到 code_patch 里的话，打上 test_patch 之后新测试会因为
    fixture 不存在而 ERROR —— 看起来像"这道题的 F2P 挂了"，其实是我们劈错了。
    """
    diff = DIFF + (
        "diff --git a/tests/conftest.py b/tests/conftest.py\n"
        "--- a/tests/conftest.py\n+++ b/tests/conftest.py\n"
        "@@ -1 +1,4 @@\n import pytest\n+\n+@pytest.fixture\n+def runner(): ...\n"
    )
    split = split_diff(diff)
    assert "tests/conftest.py" in split.test_paths
    assert "tests/conftest.py" not in split.code_paths


def test_pyproject_counts_as_protected_not_source() -> None:
    """`pyproject.toml` 在 C-42 的受保护清单里（`[tool.pytest]` 段能改变测试行为）。

    劈法必须和 `schema.py` 的校验口径一致，否则劈出来的东西根本导不进去。
    """
    diff = (
        "diff --git a/pyproject.toml b/pyproject.toml\n"
        "--- a/pyproject.toml\n+++ b/pyproject.toml\n@@ -1 +1,2 @@\n x\n+y\n"
    )
    split = split_diff(diff)
    assert split.test_paths == ("pyproject.toml",)
    assert split.code_paths == ()


def test_a_section_crossing_the_boundary_is_dropped() -> None:
    """协议 C-62：重命名时新旧路径任一受保护就整个文件丢弃。

    `src/x.py` → `tests/x.py` 这一段放哪边都不对：放 test_patch 会把源码改动
    塞进官方测试补丁，放 code_patch 又等于让被测 AI 改测试。
    """
    diff = (
        "diff --git a/src/click/helpers.py b/tests/helpers.py\n"
        "similarity index 90%\n"
        "rename from src/click/helpers.py\n"
        "rename to tests/helpers.py\n"
        "--- a/src/click/helpers.py\n+++ b/tests/helpers.py\n"
        "@@ -1 +1 @@\n-a\n+b\n"
    )
    split = split_diff(diff)
    assert split.test_patch == ""
    assert split.code_patch == ""
    assert set(split.dropped) == {"src/click/helpers.py", "tests/helpers.py"}


def test_a_pr_with_no_source_change_is_not_usable() -> None:
    """只改测试的 PR：劈完 code_patch 是空的，"只改测试就能通过"（§7.2(7)）。"""
    only_tests = DIFF[DIFF.index("diff --git a/tests") :]
    assert split_diff(only_tests).usable is False


# ══════════════════════════════════════════════════════════════
# 抽候选 F2P
# ══════════════════════════════════════════════════════════════


def test_a_brand_new_test_function_is_picked_up() -> None:
    ids = extract_f2p_candidates(split_diff(DIFF).test_patch).ids
    assert ids == ["tests/test_types.py::test_path_strips"]


def test_an_assertion_added_to_an_existing_test_is_picked_up() -> None:
    """**这一种最容易漏，而它恰恰是最常见的 bugfix 形状。**

    `def test_x` 那一行是上下文行不是新增行，只扫新增行里的 `def test_`
    会把这一类整批漏掉。
    """
    patch = (
        "diff --git a/tests/test_a.py b/tests/test_a.py\n"
        "--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def test_existing():\n"
        "     assert f(1) == 1\n"
        "+    assert f(-1) == 1\n"
    )
    assert extract_f2p_candidates(patch).ids == ["tests/test_a.py::test_existing"]


def test_a_test_inside_a_class_gets_the_class_in_its_id() -> None:
    patch = (
        "diff --git a/tests/test_a.py b/tests/test_a.py\n"
        "--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
        "@@ -1,3 +1,6 @@\n"
        " class TestPath:\n"
        "     def test_old(self):\n"
        "         pass\n"
        "+\n"
        "+    def test_new(self):\n"
        "+        assert True\n"
    )
    assert extract_f2p_candidates(patch).ids == ["tests/test_a.py::TestPath::test_new"]


def test_class_context_comes_from_the_hunk_header_when_it_is_off_screen() -> None:
    """改的是类中间的某个方法时，`class TestFoo:` 那一行不在 hunk 里。

    git 把外层的类写在 `@@ ... @@` 后面，全靠它才知道用例属于哪个类 ——
    少了类名，用例 ID 就对不上（AGENTS.md §5.5 那条坑的近亲）。
    """
    patch = (
        "diff --git a/tests/test_a.py b/tests/test_a.py\n"
        "--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
        "@@ -40,2 +40,3 @@ class TestPath:\n"
        "     def test_deep(self):\n"
        "         assert 1\n"
        "+        assert 2\n"
    )
    assert extract_f2p_candidates(patch).ids == ["tests/test_a.py::TestPath::test_deep"]


def test_async_tests_are_picked_up() -> None:
    """pytest-asyncio 的用例是 `async def`。漏了它会把一整类仓库的 F2P 抽空。"""
    patch = (
        "diff --git a/tests/test_a.py b/tests/test_a.py\n"
        "--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
        "@@ -1 +1,3 @@\n x\n+async def test_await_it():\n+    assert True\n"
    )
    assert extract_f2p_candidates(patch).ids == ["tests/test_a.py::test_await_it"]


def test_module_level_test_is_not_attributed_to_an_earlier_class() -> None:
    """类里的方法和模块级函数靠缩进区分。搞混了会拼出一个不存在的用例 ID。"""
    patch = (
        "diff --git a/tests/test_a.py b/tests/test_a.py\n"
        "--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
        "@@ -1,3 +1,7 @@\n"
        " class TestOld:\n"
        "     def test_inside(self):\n"
        "         pass\n"
        "+\n"
        "+\n"
        "+def test_outside():\n"
        "+    assert True\n"
    )
    assert extract_f2p_candidates(patch).ids == ["tests/test_a.py::test_outside"]


def test_helper_functions_are_not_candidates() -> None:
    """`def make_thing()` 不是测试。pytest 只收集 `test` 开头的。"""
    patch = (
        "diff --git a/tests/test_a.py b/tests/test_a.py\n"
        "--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
        "@@ -1 +1,3 @@\n x\n+def make_thing():\n+    return 1\n"
    )
    result = extract_f2p_candidates(patch)
    assert result.ids == []
    assert result.unattributed_lines > 0, "归不到测试名下的新增行要记账"


def test_the_same_test_is_only_listed_once() -> None:
    """一条测试被两个 hunk 各改一处时，不能出现两条一样的候选。"""
    patch = (
        "diff --git a/tests/test_a.py b/tests/test_a.py\n"
        "--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
        "@@ -1,2 +1,3 @@\n def test_x():\n     a = 1\n+    b = 2\n"
        "@@ -20,1 +21,2 @@ def test_x():\n     c = 3\n+    d = 4\n"
    )
    assert extract_f2p_candidates(patch).ids.count("tests/test_a.py::test_x") == 1


def test_deleted_lines_alone_do_not_make_a_candidate() -> None:
    """只删了几行的测试没有"新增"，不算被改出新行为。"""
    patch = (
        "diff --git a/tests/test_a.py b/tests/test_a.py\n"
        "--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
        "@@ -1,3 +1,2 @@\n def test_x():\n     a = 1\n-    b = 2\n"
    )
    assert extract_f2p_candidates(patch).ids == []


# ══════════════════════════════════════════════════════════════
# 多个关联 issue
# ══════════════════════════════════════════════════════════════


def test_several_linked_issues_are_joined() -> None:
    """题面要自足。只取一个 issue 会漏掉另一半上下文。"""
    title, body = issue_text(
        [
            {"title": "第一个", "body": "正文一"},
            {"title": "第二个", "body": "正文二"},
        ]
    )
    assert title == "第一个", "标题拼起来读着很怪，只取第一个"
    assert "正文一" in body and "正文二" in body


def test_no_linked_issue_gives_empty_strings() -> None:
    assert issue_text([]) == ("", "")
