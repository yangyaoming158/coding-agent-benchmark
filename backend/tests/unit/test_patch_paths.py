"""从 unified diff 解析改动路径的测试（协议 C-74）。

这份清单会被并进受保护路径。解析漏一个文件，那个文件就不受保护，
被测 AI 改它就生效了——而且不报错，只会让解决率莫名其妙地偏高。
"""

from __future__ import annotations

import pytest

from app.benchmark.patch_paths import derive_patch_paths


def test_simple_modification() -> None:
    diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-old\n"
        "+new\n"
    )
    assert derive_patch_paths(diff) == ["src/app.py"]


def test_content_lines_are_not_mistaken_for_headers() -> None:
    """hunk 内容里出现 `--- foo` 时不能被当成文件头。

    删掉一行 `-- foo`，diff 里那行就长成 `--- foo`。按行首前缀 grep 的写法
    会凭空多出一个"被改的文件"，那个文件还会被并进受保护路径清单。
    所以解析器按 hunk 头声明的行数精确数过去。
    """
    diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,3 +1,3 @@\n"
        " keep\n"
        "--- foo\n"
        "+++ bar\n"
    )
    assert derive_patch_paths(diff) == ["src/app.py"]


def test_new_file_skips_dev_null() -> None:
    diff = (
        "diff --git a/tests/fixtures/data.json b/tests/fixtures/data.json\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/tests/fixtures/data.json\n"
        "@@ -0,0 +1,1 @@\n"
        "+{}\n"
    )
    assert derive_patch_paths(diff) == ["tests/fixtures/data.json"]


def test_deleted_file_skips_dev_null() -> None:
    diff = (
        "diff --git a/old.py b/old.py\n"
        "deleted file mode 100644\n"
        "--- a/old.py\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-gone\n"
    )
    assert derive_patch_paths(diff) == ["old.py"]


def test_rename_records_both_paths() -> None:
    """重命名要新旧路径都记（C-74 第 4 条）。

    只记新路径的话，把 `tests/test_a.py` 改名成 `helper.py` 再改内容就绕过
    路径匹配了——这正是 C-62 要挡的作弊方式。
    """
    diff = (
        "diff --git a/tests/test_old.py b/tests/test_new.py\n"
        "similarity index 92%\n"
        "rename from tests/test_old.py\n"
        "rename to tests/test_new.py\n"
    )
    assert derive_patch_paths(diff) == ["tests/test_new.py", "tests/test_old.py"]


def test_copy_records_both_paths() -> None:
    diff = (
        "diff --git a/tests/base.py b/tests/copy.py\n"
        "copy from tests/base.py\n"
        "copy to tests/copy.py\n"
    )
    assert derive_patch_paths(diff) == ["tests/base.py", "tests/copy.py"]


def test_mode_change_only() -> None:
    """只改文件权限，没有 ---/+++ 行，靠 `diff --git` 那行拿路径。"""
    diff = "diff --git a/scripts/run.sh b/scripts/run.sh\nold mode 100644\nnew mode 100755\n"
    assert derive_patch_paths(diff) == ["scripts/run.sh"]


def test_multiple_files_sorted_and_deduped() -> None:
    """多文件补丁排序去重（C-74 第 3 条）。"""
    diff = (
        "diff --git a/z.py b/z.py\n--- a/z.py\n+++ b/z.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    )
    assert derive_patch_paths(diff) == ["a.py", "z.py"]


def test_quoted_path_with_chinese_name() -> None:
    r"""路径里有非 ASCII 时 git 会写成 `"a/\346\265\213.py"`，八进制转义要还原。

    不还原的话，中文文件名会变成一串反斜杠数字，和题目里存的真实路径对不上，
    C-74 第 6 条的防篡改校验就会对好题误报。
    """
    diff = (
        'diff --git "a/tests/\\346\\265\\213\\350\\257\\225.py" '
        '"b/tests/\\346\\265\\213\\350\\257\\225.py"\n'
        '--- "a/tests/\\346\\265\\213\\350\\257\\225.py"\n'
        '+++ "b/tests/\\346\\265\\213\\350\\257\\225.py"\n'
        "@@ -1,1 +1,1 @@\n"
        "-a\n"
        "+b\n"
    )
    assert derive_patch_paths(diff) == ["tests/测试.py"]


def test_hunk_without_line_counts() -> None:
    """`@@ -1 +1 @@` 省略了 `,n`，按 1 行算。"""
    diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert derive_patch_paths(diff) == ["x.py"]


@pytest.mark.parametrize("diff", ["", "   \n", "这不是补丁"])
def test_non_patch_input_yields_nothing(diff: str) -> None:
    """空补丁和垃圾输入都返回空列表，不抛异常。

    空补丁是合法输入（被测 AI 可能什么都没改），要不要因此拒收是调用方的判断。
    """
    assert derive_patch_paths(diff) == []


def test_paths_are_normalized() -> None:
    """`./` 前缀和反斜杠都要归一化，不然匹配不上 `tests/**`。"""
    diff = (
        "diff --git a/./tests//test_a.py b/./tests//test_a.py\n"
        "--- a/./tests//test_a.py\n"
        "+++ b/./tests//test_a.py\n"
        "@@ -1,1 +1,1 @@\n-a\n+b\n"
    )
    assert derive_patch_paths(diff) == ["tests/test_a.py"]
