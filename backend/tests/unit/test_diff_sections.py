"""按文件段拆 diff 的测试（E3-T3 的解析基础）。

拆错的后果分两种，都很难查：

- **段边界错**：两个文件的改动黏成一段，丢掉受保护的那个时会把旁边合法的
  源码改动一起丢掉 —— AI 的修复被悄悄删了，判成"没修好"。
- **段内容错**：重新拼出来的补丁和原文不一致，`git apply` 报 corrupt patch，
  判成 INVALID_PATCH，而责任其实在平台。

所以这一组里最重要的是"拼回去等于原文"那几条。
"""

from __future__ import annotations

from app.domain.patch_paths import iter_diff_sections, iter_patch_paths

MODIFY = (
    "diff --git a/src/app.py b/src/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def f():\n"
    "-    return 1\n"
    "+    return 2\n"
)

NEW_FILE = (
    "diff --git a/src/added.py b/src/added.py\n"
    "new file mode 100644\n"
    "index 0000000..3333333\n"
    "--- /dev/null\n"
    "+++ b/src/added.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+x = 1\n"
    "+y = 2\n"
)

MODE_ONLY = "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n"

BINARY = (
    "diff --git a/logo.png b/logo.png\n"
    "index 6772730..3fa429c 100644\n"
    "Binary files a/logo.png and b/logo.png differ\n"
)

RENAME = (
    "diff --git a/tests/test_a.py b/helper.py\n"
    "similarity index 92%\n"
    "rename from tests/test_a.py\n"
    "rename to helper.py\n"
)


def test_each_file_becomes_one_section() -> None:
    sections = list(iter_diff_sections(MODIFY + NEW_FILE + MODE_ONLY))
    assert [s.paths for s in sections] == [("src/app.py",), ("src/added.py",), ("run.sh",)]


def test_sections_join_back_into_the_original() -> None:
    """拼回去必须逐字节等于原文。

    这是整个归一化能成立的前提：留下来的段原样拼接，所以输出必然还是合法补丁。
    """
    raw = MODIFY + NEW_FILE + BINARY + RENAME
    assert "".join(s.text for s in iter_diff_sections(raw)) == raw


def test_counts_added_and_deleted_lines() -> None:
    section = next(iter(iter_diff_sections(MODIFY)))
    assert (section.lines_added, section.lines_deleted) == (1, 1)
    assert section.hunk_count == 1


def test_hunk_content_that_looks_like_a_header_does_not_split_the_section() -> None:
    """删掉一行 `-- foo`，diff 里就是 `--- foo`。它不能被当成新文件的开头。

    当成新文件的话，这一段会被劈成两半：前半有 hunk 头、后半没有，
    两半都不是合法补丁，而且凭空多出一个"被改的文件"。
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
    sections = list(iter_diff_sections(diff))
    assert len(sections) == 1
    assert sections[0].paths == ("src/app.py",)


def test_plain_diff_without_git_headers_splits_on_the_old_header() -> None:
    """`diff -u` 的输出没有 `diff --git` 行，只能靠"又来一个 `---`"分段。"""
    diff = (
        "--- a/one.py\n+++ b/one.py\n@@ -1 +1 @@\n-a\n+b\n"
        "--- a/two.py\n+++ b/two.py\n@@ -1 +1 @@\n-c\n+d\n"
    )
    assert [s.paths for s in iter_diff_sections(diff)] == [("one.py",), ("two.py",)]


def test_mode_only_section_is_recognized() -> None:
    section = next(iter(iter_diff_sections(MODE_ONLY)))
    assert section.has_mode_change
    assert section.is_mode_change_only
    assert not section.has_content


def test_mode_change_with_real_edits_is_not_mode_only() -> None:
    """又改权限又改内容的段是真改动，不能当成空 mode 变更丢掉。"""
    diff = (
        "diff --git a/run.sh b/run.sh\n"
        "old mode 100644\n"
        "new mode 100755\n"
        "--- a/run.sh\n"
        "+++ b/run.sh\n"
        "@@ -1 +1 @@\n"
        "-echo old\n"
        "+echo new\n"
    )
    section = next(iter(iter_diff_sections(diff)))
    assert section.has_mode_change
    assert not section.is_mode_change_only
    assert section.has_content


def test_binary_markers_are_recognized() -> None:
    """`git diff` 默认打印 `Binary files ...`，加 `--binary` 打印 `GIT binary patch`。"""
    assert next(iter(iter_diff_sections(BINARY))).is_binary
    with_data = (
        "diff --git a/logo.png b/logo.png\nindex 6772730..3fa429c 100644\nGIT binary patch\n"
    )
    assert next(iter(iter_diff_sections(with_data))).is_binary


def test_rename_section_carries_both_paths() -> None:
    """改名要把旧路径和新路径都收进来 —— 协议 C-62 两个都要参与受保护判断。

    只记新路径的话，AI 把 `tests/test_a.py` 改名成 `helper.py` 就绕过去了。
    """
    section = next(iter(iter_diff_sections(RENAME)))
    assert section.paths == ("helper.py", "tests/test_a.py")
    assert section.has_file_operation
    assert section.has_content, "纯改名没有 hunk，但它是真改动，不能当空段丢掉"


def test_new_file_section_has_content_and_file_operation() -> None:
    section = next(iter(iter_diff_sections(NEW_FILE)))
    assert section.has_file_operation
    assert section.has_content
    assert section.lines_added == 2


def test_preamble_before_the_first_file_is_dropped() -> None:
    """`git format-patch` 会在前面写邮件头和提交信息，那些不属于任何文件。

    留着的话，同一份改动因为提交信息不同会算出不同的补丁哈希。
    """
    raw = (
        "From 1234567 Mon Sep 17 00:00:00 2001\n"
        "From: someone <a@b.c>\n"
        "Subject: [PATCH] fix it\n"
        "\n"
        "正文说明\n"
        "---\n"  # 注意这行只有三个横杠，不是 `--- ` 文件头
        " src/app.py | 2 +-\n"
        "\n" + MODIFY
    )
    sections = list(iter_diff_sections(raw))
    assert [s.paths for s in sections] == [("src/app.py",)]
    assert sections[0].text == MODIFY


def test_crlf_structural_lines_are_normalized_content_lines_are_not() -> None:
    """结构行的行尾统一成 LF；内容行原样保留。

    内容行不能动：`+foo\\r` 有两种可能 —— 补丁文件本身是 CRLF 存的，
    或者这一行真的要往文件里写一个 CR。从补丁里分不出来，猜错第二种
    就是悄悄改掉了 AI 的修改内容。结构行没有这个歧义。
    """
    crlf = MODIFY.replace("\n", "\r\n")
    section = next(iter(iter_diff_sections(crlf)))

    lines = section.text.split("\n")
    assert lines[0] == "diff --git a/src/app.py b/src/app.py", "结构行不该留 \\r"
    assert "@@ -1,2 +1,2 @@" in lines
    assert "+    return 2\r" in lines, "内容行的 \\r 要原样留着"


def test_iter_patch_paths_still_walks_every_section() -> None:
    """路径解析改成走段之后，行为要和原来一致（原有 14 条用例是主要证据）。"""
    raw = MODIFY + RENAME
    assert list(iter_patch_paths(raw)) == [
        "src/app.py",
        "src/app.py",
        "src/app.py",
        "src/app.py",
        "tests/test_a.py",
        "helper.py",
        "tests/test_a.py",
        "helper.py",
    ]


def test_empty_input_yields_no_sections() -> None:
    assert list(iter_diff_sections("")) == []
    assert list(iter_diff_sections("这不是一个补丁\n随便写点什么\n")) == []
