"""GitHub 挖掘器的判断逻辑（E1-T4，`03-benchmark-spec.md` §8.4）。

**这一组不联网。** 真正花时间的是打 GraphQL，但那是薄薄一层胶水；
靠不住的是它两头的判断：窗口切得全不全、哪个 PR 算候选、进度接不接得上。
那些都是纯函数，在这里全测掉。

三处最值得测，因为**错了不会报错**：

1. **窗口不能有缝也不能重叠。** 有缝就漏 PR，重叠就白花配额，两种都不报错，
   表现只是"产出率比上次低了一点"。
2. **`base_commit` 取 `parents[0]` 什么时候靠不住。** rebase merge 算出来的
   base_commit 偏后，工作区里会已经带上这个 PR 的一部分改动 —— 题目看起来正常，
   只是被测 AI 拿到的代码已经修好一半了。
3. **续跑的参数校验。** 换了页大小接着走会跳过一段 PR，同样不报错。
"""

from __future__ import annotations

import json
from datetime import date
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from app.benchmark import gh_cache
from app.benchmark.mining import (
    DEFAULT_PAGE_SIZE,
    DEGRADED_RETRIES,
    MINER_VERSION,
    REJECT_FILES_TRUNCATED,
    REJECT_NO_BASE_COMMIT,
    REJECT_NO_LINKED_ISSUE,
    REJECT_NO_SOURCE_FILES,
    REJECT_NO_TEST_FILES,
    MiningState,
    WindowState,
    bisect_window,
    build_payload,
    classify,
    default_since,
    funnel,
    new_state,
    parse_pr_node,
    search_query,
    split_window,
    summarize,
)

# ══════════════════════════════════════════════════════════════
# 时间窗
# ══════════════════════════════════════════════════════════════


def test_split_window_covers_range_without_gap_or_overlap() -> None:
    """切出来的窗口首尾相接：不漏一天，也不重复一天。

    `merged:A..B` 两端都是闭区间，所以相邻窗口差一天正好接上。
    差错一天的话，重叠会白花配额，留缝会漏掉那天合并的 PR —— 两种都不报错。
    """
    since, until = date(2024, 9, 9), date(2026, 9, 9)
    windows = split_window(since, until, 12)

    assert windows[0][0] == since
    assert windows[-1][1] == until
    for (_, prev_stop), (next_start, _) in pairwise(windows):
        assert (next_start - prev_stop).days == 1


def test_split_window_makes_the_requested_number_of_buckets() -> None:
    assert len(split_window(date(2024, 1, 1), date(2025, 12, 31), 12)) == 12
    assert len(split_window(date(2024, 1, 1), date(2025, 12, 31), 6)) == 6


def test_split_window_never_makes_empty_windows() -> None:
    """天数比段数还少时段数自动缩小 —— 零长度的窗口只会白发一次请求。"""
    windows = split_window(date(2026, 9, 1), date(2026, 9, 3), 12)
    assert len(windows) == 3
    assert all(start <= stop for start, stop in windows)


def test_split_window_rejects_backwards_range() -> None:
    assert split_window(date(2026, 9, 9), date(2026, 9, 1), 4) == []


def test_bisect_window_splits_contiguously() -> None:
    """二分出来的两半也要首尾相接。窗口撞到搜索 1000 条上限时靠它。"""
    halves = bisect_window(date(2025, 1, 1), date(2025, 1, 31))
    assert halves is not None
    (a_start, a_stop), (b_start, b_stop) = halves
    assert a_start == date(2025, 1, 1)
    assert b_stop == date(2025, 1, 31)
    assert (b_start - a_stop).days == 1


def test_bisect_window_gives_up_on_a_single_day() -> None:
    """一天再也切不动了。这时只能承认拿不全，报一条 problem。"""
    assert bisect_window(date(2025, 1, 1), date(2025, 1, 1)) is None


def test_default_since_covers_two_years_inclusive() -> None:
    until = date(2026, 9, 9)
    since = default_since(until, 730)
    assert (until - since).days + 1 == 730


def test_search_query_uses_the_strict_linked_issue_form() -> None:
    """只认 GitHub 的 `linked:issue`。

    §8.8 实测过放宽口径（正文里出现 `#123` 也算）：pyecharts 1→6，
    nonebot2 反而更低（8→4），akshare 宽口径抽 150 个一个合格的都没有。
    瓶颈是"带测试的 bugfix PR"稀少，不是关联方式。
    """
    q = search_query("pallets/click", "2025-01-01", "2025-06-30")
    assert q == "repo:pallets/click is:pr is:merged linked:issue merged:2025-01-01..2025-06-30"


# ══════════════════════════════════════════════════════════════
# GraphQL 节点 → 候选
# ══════════════════════════════════════════════════════════════


def pr_node(**overrides: Any) -> dict[str, Any]:
    """一个典型的 PR 节点，形状抄自 2026-09-09 真跑 GitHub 拿到的返回。"""
    node: dict[str, Any] = {
        "number": 2946,
        "title": "Expect alternate errors in test_file_surrogates()",
        "url": "https://github.com/pallets/click/pull/2946",
        "createdAt": "2025-05-25T21:36:12Z",
        "mergedAt": "2025-05-26T20:17:50Z",
        "baseRefName": "main",
        "baseRefOid": "2d610e36a429bfebf0adb0ca90cdc0585f296369",
        "additions": 6,
        "deletions": 1,
        "changedFiles": 2,
        "commits": {"totalCount": 1},
        "mergeCommit": {
            "oid": "b63239d657aab7c4e04667a62e7177f617b37230",
            "parents": {
                "totalCount": 2,
                "nodes": [
                    # parents[0] 属于**别的** PR，说明它是基线上的前一个提交
                    {
                        "oid": "2d610e36a429bfebf0adb0ca90cdc0585f296369",
                        "associatedPullRequests": {"nodes": [{"number": 2935}]},
                    },
                    {
                        "oid": "7559bb8fb82f7bcb923bcb6b900c11472c6f227f",
                        "associatedPullRequests": {"nodes": [{"number": 2946}]},
                    },
                ],
            },
        },
        "author": {"login": "somebody"},
        "files": {
            "totalCount": 2,
            "nodes": [
                {"path": "tests/test_types.py", "additions": 6, "deletions": 1},
                {"path": "src/click/types.py", "additions": 3, "deletions": 2},
            ],
        },
        "closingIssuesReferences": {
            "totalCount": 1,
            "nodes": [
                {
                    "number": 2945,
                    "title": "test_file_surrogates() covers not all file-system types",
                    "body": "复现步骤：\n\n```python\nPath('x').open('r')\n```\n",
                    "url": "https://github.com/pallets/click/issues/2945",
                    "createdAt": "2025-05-25T21:31:27Z",
                    "state": "CLOSED",
                }
            ],
        },
    }
    node.update(overrides)
    return node


def test_parse_pr_node_reads_the_fields_we_care_about() -> None:
    pr = parse_pr_node(pr_node())
    assert pr is not None
    assert pr.number == 2946
    assert pr.paths == ("tests/test_types.py", "src/click/types.py")
    assert pr.test_paths == ("tests/test_types.py",)
    assert pr.source_paths == ("src/click/types.py",)
    assert pr.issue_number == 2945
    assert pr.issues[0].body.startswith("复现步骤")


def test_parse_pr_node_survives_every_nullable_field() -> None:
    """GraphQL 里 `author`（用户注销了）、`mergeCommit`、`files` 都可能是 null。

    漏判一个会在翻到第几百页时崩掉，而那时前面的进度还没落盘。
    """
    pr = parse_pr_node(
        pr_node(
            author=None,
            mergeCommit=None,
            files=None,
            closingIssuesReferences=None,
        )
    )
    assert pr is not None
    assert pr.author is None
    assert pr.merge_commit_oid is None
    assert pr.merge_parents == ()
    assert pr.paths == ()
    assert pr.issues == ()
    assert pr.base_commit is None


def test_parse_pr_node_ignores_non_pull_request_nodes() -> None:
    """search 的返回里会混进空对象（不是 PullRequest 的那些）。"""
    assert parse_pr_node(None) is None
    assert parse_pr_node({}) is None


def test_base_commit_is_the_first_parent() -> None:
    """§8.4：取 `parents[0]`。普通 merge commit 的 parents[0] 就是合并时的基线。"""
    pr = parse_pr_node(pr_node())
    assert pr is not None
    assert pr.base_commit == "2d610e36a429bfebf0adb0ca90cdc0585f296369"
    assert pr.suspect_base_commit is False


def test_a_squash_merge_is_not_suspect_even_with_many_commits() -> None:
    """squash merge：一个父提交，它是基线上的前一个提交，属于**别的** PR。

    这一条是防止误报的：按"父提交只有一个 + PR 有多个提交"判的话，
    squash merge 会被整批冤枉。2026-09-09 实测 pallets/click 一个窗口
    16 条候选，按那个判据 15 条"可疑" —— 一个 94% 命中的告警等于没有告警。
    """
    node = pr_node(
        commits={"totalCount": 4},
        mergeCommit={
            "oid": "a" * 40,
            "parents": {
                "totalCount": 1,
                "nodes": [
                    {"oid": "b" * 40, "associatedPullRequests": {"nodes": [{"number": 2900}]}}
                ],
            },
        },
    )
    pr = parse_pr_node(node)
    assert pr is not None
    assert pr.base_commit == "b" * 40
    assert pr.suspect_base_commit is False


def test_a_rebase_merge_is_flagged() -> None:
    """rebase merge：`parents[0]` **也属于本 PR**，说明它是被 rebase 进来的，不是基线。

    这种 PR 算出来的 base_commit 偏后，工作区里会已经带上这个 PR 的一部分改动 ——
    题目看起来正常，只是被测 AI 拿到的代码已经修好一半了。
    只报可疑、不丢弃，真正的兜底是 E1-T3 验证流水线的 S5（`F2P_NOT_FAILING`）。
    """
    node = pr_node(
        commits={"totalCount": 4},
        mergeCommit={
            "oid": "a" * 40,
            "parents": {
                "totalCount": 1,
                "nodes": [
                    {"oid": "b" * 40, "associatedPullRequests": {"nodes": [{"number": 2946}]}}
                ],
            },
        },
    )
    pr = parse_pr_node(node)
    assert pr is not None
    assert pr.suspect_base_commit is True


def test_a_parent_with_no_associated_pull_request_is_not_suspect() -> None:
    """直接推到主干的提交没有关联 PR。取不到不等于可疑。"""
    node = pr_node(
        mergeCommit={"oid": "a" * 40, "parents": {"totalCount": 1, "nodes": [{"oid": "b" * 40}]}}
    )
    pr = parse_pr_node(node)
    assert pr is not None
    assert pr.suspect_base_commit is False


def test_config_only_changes_count_as_neither_test_nor_source() -> None:
    """`pyproject.toml` 既不是测试用例，也不是被测 AI 能改的源码。

    这一条是 §8.8 记的口径：判"带了测试改动"要用 `TEST_CODE_PATTERNS`，
    不是整份 `DEFAULT_PROTECTED_PATTERNS`（后者含 pyproject.toml、.github/**）。
    """
    node = pr_node(
        files={
            "totalCount": 2,
            "nodes": [{"path": "pyproject.toml"}, {"path": ".github/workflows/ci.yaml"}],
        }
    )
    pr = parse_pr_node(node)
    assert pr is not None
    assert pr.test_paths == ()
    assert pr.source_paths == ()


# ══════════════════════════════════════════════════════════════
# 三条自动过滤（§8.4）
# ══════════════════════════════════════════════════════════════


def test_classify_accepts_a_normal_bugfix_pr() -> None:
    pr = parse_pr_node(pr_node())
    assert pr is not None
    assert classify(pr) is None


def test_classify_rejects_a_pr_without_a_linked_issue() -> None:
    """没关联 issue 就没有题面。查询里带了 `linked:issue`，
    但 issue 被删掉之后 GitHub 仍然会把 PR 搜出来，返回里 totalCount 是 0。
    """
    pr = parse_pr_node(pr_node(closingIssuesReferences={"totalCount": 0, "nodes": []}))
    assert pr is not None
    assert classify(pr) == REJECT_NO_LINKED_ISSUE


def test_classify_rejects_a_pr_that_changes_no_tests() -> None:
    """没测试就没有 F2P，题目立不起来（§7.2(7)）。"""
    pr = parse_pr_node(pr_node(files={"totalCount": 1, "nodes": [{"path": "src/click/types.py"}]}))
    assert pr is not None
    assert classify(pr) == REJECT_NO_TEST_FILES


def test_classify_rejects_a_pr_that_changes_no_source() -> None:
    """只改测试的 PR：劈完补丁之后 `gold_patch` 会是空的，"只改测试就能通过"。"""
    pr = parse_pr_node(pr_node(files={"totalCount": 1, "nodes": [{"path": "tests/test_types.py"}]}))
    assert pr is not None
    assert classify(pr) == REJECT_NO_SOURCE_FILES


def test_classify_rejects_a_truncated_file_list_it_cannot_judge() -> None:
    """PR 改了 100 个以上的文件，我们只看得到前 100 个。

    前 100 个里判不出结论时丢弃 —— 当成候选是在瞎猜，
    当成"没改测试"又可能冤枉它。
    """
    pr = parse_pr_node(
        pr_node(files={"totalCount": 260, "nodes": [{"path": "src/click/types.py"}]})
    )
    assert pr is not None
    assert pr.files_truncated is True
    assert classify(pr) == REJECT_FILES_TRUNCATED


def test_classify_keeps_a_truncated_pr_whose_verdict_is_already_settled() -> None:
    """前 100 个里既有测试又有源码：结论已经成立，只是路径清单不全。收下，并打标。"""
    pr = parse_pr_node(
        pr_node(
            files={
                "totalCount": 260,
                "nodes": [{"path": "tests/test_types.py"}, {"path": "src/click/types.py"}],
            }
        )
    )
    assert pr is not None
    assert pr.files_truncated is True
    assert classify(pr) is None


def test_classify_rejects_a_pr_with_no_base_commit() -> None:
    pr = parse_pr_node(pr_node(mergeCommit=None))
    assert pr is not None
    assert classify(pr) == REJECT_NO_BASE_COMMIT


# ══════════════════════════════════════════════════════════════
# raw_payload：E1-T4 和 E1-T5 的交接面
# ══════════════════════════════════════════════════════════════


def test_payload_keeps_the_issue_body_exactly_as_github_gave_it() -> None:
    """**这一步只抄，不改。** 脱敏是 E1-T5 的活。

    在这里顺手脱敏的话，E1-T5 就再也看不到原文、没法核对自己脱干净了没有 ——
    而它的 AC 正是"脱敏后不含仓库 PR 链接与 40 位 hash（正则断言）"。
    """
    pr = parse_pr_node(pr_node())
    assert pr is not None
    payload = build_payload(pr, repo="pallets/click")

    body = payload["issues"][0]["body"]
    assert "```python" in body, "围栏代码块要留着 —— 判语言和脱敏都要先剥它"
    assert payload["miner_version"] == MINER_VERSION


def test_payload_carries_no_diff_text() -> None:
    """不存 diff 正文：拆 test_patch/code_patch 是 E1-T5 的活，
    而一个 PR 的 diff 动辄几十上百 KB，塞进 JSONB 会让这张表很难查。
    """
    pr = parse_pr_node(pr_node())
    assert pr is not None
    blob = json.dumps(build_payload(pr, repo="pallets/click"), ensure_ascii=False)
    assert "diff --git" not in blob


def test_payload_records_everything_needed_to_second_guess_base_commit() -> None:
    """把 base_commit 的几种来源都存下来，E1-T5 才有料可判。"""
    pr = parse_pr_node(pr_node())
    assert pr is not None
    payload = build_payload(pr, repo="pallets/click")
    assert payload["base_commit"] == "2d610e36a429bfebf0adb0ca90cdc0585f296369"
    assert payload["base_commit_suspect"] is False
    assert len(payload["pr"]["merge_parents"]) == 2
    assert payload["pr"]["base_ref_oid"]
    assert payload["pr"]["base_ref_name"] == "main"


def test_payload_splits_paths_for_the_next_step() -> None:
    pr = parse_pr_node(pr_node())
    assert pr is not None
    files = build_payload(pr, repo="pallets/click")["files"]
    assert files["test_paths"] == ["tests/test_types.py"]
    assert files["source_paths"] == ["src/click/types.py"]


def test_lowest_issue_number_wins_when_several_are_linked() -> None:
    """多个关联 issue 时取号最小的：先被提的通常才是那份 bug 报告。
    全部关联 issue 仍然进 raw_payload，真要挑还是 E1-T5 的事。
    """
    pr = parse_pr_node(
        pr_node(
            closingIssuesReferences={
                "totalCount": 2,
                "nodes": [
                    {"number": 3100, "title": "b", "body": "", "url": "u2"},
                    {"number": 2945, "title": "a", "body": "", "url": "u1"},
                ],
            }
        )
    )
    assert pr is not None
    assert pr.issue_number == 2945
    assert len(build_payload(pr, repo="x")["issues"]) == 2


# ══════════════════════════════════════════════════════════════
# 进度状态
# ══════════════════════════════════════════════════════════════


def make_state() -> MiningState:
    return new_state(
        "pallets/click", since=date(2024, 9, 9), until=date(2026, 9, 9), buckets=4, page_size=25
    )


def test_state_round_trips_through_json() -> None:
    """进度要能落盘再读回来 —— 这就是"可续跑"的全部实现。"""
    state = make_state()
    state.windows[0].done = True
    state.windows[0].prs_seen = 40
    state.windows[0].candidates = 9
    state.count_reject(REJECT_NO_TEST_FILES)
    state.problems.append("某个窗口一天就超 1000 条")

    restored = MiningState.from_json(json.loads(json.dumps(state.to_json())))
    assert restored.windows[0].done is True
    assert restored.candidates == 9
    assert restored.rejects == {REJECT_NO_TEST_FILES: 1}
    assert restored.problems == ["某个窗口一天就超 1000 条"]
    assert restored.page_size == 25


def test_pending_skips_finished_windows() -> None:
    state = make_state()
    state.windows[0].done = True
    assert len(state.pending()) == 3
    assert state.windows_done == 1
    assert state.finished is False


def test_replace_window_keeps_the_windows_in_time_order() -> None:
    """二分出来的两半就地替换，不追加到末尾 —— 否则进度看起来在时间上乱跳。"""
    state = make_state()
    target = state.windows[1]
    state.replace_window(target, [("2025-03-01", "2025-05-31"), ("2025-06-01", "2025-08-31")])

    assert len(state.windows) == 5
    assert state.windows[1].start == "2025-03-01"
    assert state.windows[2].start == "2025-06-01"
    starts = [w.start for w in state.windows]
    assert starts == sorted(starts)


def test_yield_rate_is_candidates_over_prs_seen() -> None:
    """AC 里那张"候选产出率报表"的主指标。"""
    state = make_state()
    state.windows[0].prs_seen = 100
    state.windows[0].candidates = 21
    state.windows[1].prs_seen = 100
    state.windows[1].candidates = 19
    assert state.yield_rate == pytest.approx(0.20)


def test_yield_rate_is_none_before_anything_is_scanned() -> None:
    """一个都没扫过时不编 0 出来 —— 0% 和"还没开始"是两回事。"""
    assert make_state().yield_rate is None


def test_funnel_starts_at_scanned_and_ends_at_candidates() -> None:
    state = make_state()
    state.windows[0].prs_seen = 50
    state.windows[0].candidates = 5
    state.count_reject(REJECT_NO_TEST_FILES)
    rows = funnel(state)
    assert rows[0] == ("扫过的 PR", 50)
    assert rows[-1] == ("候选", 5)
    assert ("没改测试", 1) in rows


def test_summarize_adds_up_several_repos() -> None:
    a, b = make_state(), make_state()
    a.windows[0].prs_seen, a.windows[0].candidates = 100, 30
    b.windows[0].prs_seen, b.windows[0].candidates = 100, 10
    total = summarize([a, b])
    assert total["prs_seen"] == 200
    assert total["candidates"] == 40
    assert total["yield_rate"] == pytest.approx(0.20)


# ══════════════════════════════════════════════════════════════
# 续跑的参数校验
# ══════════════════════════════════════════════════════════════


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import cli.mine as mine

    monkeypatch.setattr(mine, "STATE_DIR", tmp_path / "mining")
    return tmp_path / "mining"


def test_resume_picks_up_where_it_left_off(state_dir: Path) -> None:
    from cli.mine import load_state, resume_or_new, save_state

    args = {
        "since": date(2024, 9, 9),
        "until": date(2026, 9, 9),
        "buckets": 4,
        "page_size": DEFAULT_PAGE_SIZE,
    }
    state = resume_or_new("pallets/click", restart=False, **args)  # type: ignore[arg-type]
    state.windows[0].done = True
    state.windows[0].candidates = 7
    save_state(state)

    assert load_state("pallets/click") is not None
    resumed = resume_or_new("pallets/click", restart=False, **args)  # type: ignore[arg-type]
    assert resumed.windows_done == 1
    assert resumed.candidates == 7


def test_resume_refuses_when_the_parameters_changed(state_dir: Path) -> None:
    """换了页大小接着走会**跳过一段 PR 而且不报错** —— GitHub 的游标是按页大小算偏移的。

    这种错查起来极难，表现只是"产出率莫名其妙比上次低"。所以宁可当场拒绝，
    让人显式加 `--restart`。
    """
    from cli.mine import resume_or_new, save_state

    save_state(
        resume_or_new(
            "pallets/click",
            since=date(2024, 9, 9),
            until=date(2026, 9, 9),
            buckets=4,
            page_size=25,
            restart=False,
        )
    )
    with pytest.raises(SystemExit, match="对不上"):
        resume_or_new(
            "pallets/click",
            since=date(2024, 9, 9),
            until=date(2026, 9, 9),
            buckets=4,
            page_size=50,
            restart=False,
        )


def test_restart_throws_the_old_progress_away(state_dir: Path) -> None:
    from cli.mine import resume_or_new, save_state

    state = resume_or_new(
        "pallets/click",
        since=date(2024, 9, 9),
        until=date(2026, 9, 9),
        buckets=4,
        page_size=25,
        restart=False,
    )
    state.windows[0].done = True
    save_state(state)

    fresh = resume_or_new(
        "pallets/click",
        since=date(2024, 1, 1),
        until=date(2026, 9, 9),
        buckets=6,
        page_size=50,
        restart=True,
    )
    assert fresh.windows_done == 0
    assert len(fresh.windows) == 6


# ══════════════════════════════════════════════════════════════
# 文件缓存
# ══════════════════════════════════════════════════════════════

QUERY = "query($q: String!) { search(query: $q) { issueCount } }"


def test_cache_key_ignores_the_order_variables_are_written_in() -> None:
    """同一个查询换个参数写法不该变成两条缓存。"""
    a = gh_cache.cache_key("graphql", QUERY, {"q": "x", "count": 25})
    b = gh_cache.cache_key("graphql", QUERY, {"count": 25, "q": "x"})
    assert a == b


def test_cache_key_changes_when_the_query_changes() -> None:
    """改了查询就该重新拉 —— 拿旧结果套新字段会静默少数据。"""
    a = gh_cache.cache_key("graphql", QUERY, {"q": "x"})
    b = gh_cache.cache_key("graphql", QUERY + " # 多取一个字段", {"q": "x"})
    assert a != b


def test_cache_round_trip(tmp_path: Path) -> None:
    key = gh_cache.cache_key("graphql", QUERY, {"q": "x"})
    gh_cache.write(key, {"search": {"issueCount": 9}}, root=tmp_path)
    assert gh_cache.read(key, root=tmp_path) == {"search": {"issueCount": 9}}
    assert gh_cache.cache_size(tmp_path) == (
        1,
        gh_cache.cache_path(key, root=tmp_path).stat().st_size,
    )


def test_cache_expires(tmp_path: Path) -> None:
    key = gh_cache.cache_key("graphql", QUERY, {"q": "x"})
    gh_cache.write(key, {"a": 1}, root=tmp_path)
    assert gh_cache.read(key, ttl_s=0, root=tmp_path) is None


def test_a_broken_cache_file_counts_as_a_miss(tmp_path: Path) -> None:
    """缓存是纯优化。为一个坏文件中断挖掘不值得 ——
    大不了重新拉一次，而抛出去会让上一个窗口烧掉的配额白花。
    """
    key = gh_cache.cache_key("graphql", QUERY, {"q": "x"})
    path = gh_cache.cache_path(key, root=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    assert gh_cache.read(key, root=tmp_path) is None


def test_cached_graphql_only_calls_the_network_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二次问同一个问题走缓存。**命中时配额返回 None** ——
    没发请求就没有配额消耗，编一个 0 出来会让"这次挖掘烧了多少点"失真。
    """
    calls = []

    def fake_graphql(query: str, variables: Any = None, **kwargs: Any) -> tuple[Any, Any]:
        calls.append(query)
        return {"search": {"issueCount": 9}}, None

    monkeypatch.setattr(gh_cache, "graphql", fake_graphql)
    stats = gh_cache.CacheStats()

    first = gh_cache.cached_graphql(QUERY, {"q": "x"}, root=tmp_path, stats=stats)
    second = gh_cache.cached_graphql(QUERY, {"q": "x"}, root=tmp_path, stats=stats)

    assert len(calls) == 1
    assert first[2] is False and second[2] is True
    assert second[1] is None
    assert (stats.hits, stats.misses) == (1, 1)


def test_no_cache_bypasses_the_file_entirely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-cache` 传 `ttl_s < 0`，既不读也不写。"""
    monkeypatch.setattr(gh_cache, "graphql", lambda *a, **k: ({"search": {"issueCount": 1}}, None))
    gh_cache.cached_graphql(QUERY, {"q": "x"}, ttl_s=-1, root=tmp_path)
    assert gh_cache.cache_size(tmp_path) == (0, 0)


def test_window_state_survives_unknown_fields() -> None:
    """旧进度文件里多出来的字段不该让续跑崩掉。"""
    restored = WindowState.from_json(
        {"start": "2025-01-01", "stop": "2025-01-31", "future_field": 1}
    )
    assert restored.start == "2025-01-01"
    assert restored.done is False


# ══════════════════════════════════════════════════════════════
# 挖掘主循环（不联网，`cached_graphql` 换成假的）
#
# 这一段测的全是"错了不报错"的分支：窗口太宽没二分就漏 PR，
# 降级响应照收就静默丢一整页，配额烧光不停就白跑。
# ══════════════════════════════════════════════════════════════


def page(
    *,
    issue_count: int,
    numbers: list[int],
    has_next: bool = False,
    cursor: str | None = None,
    remaining: int = 4000,
) -> dict[str, Any]:
    """造一页 GraphQL 返回。`numbers` 是这一页有哪几个 PR。"""
    return {
        "rateLimit": {"cost": 1, "remaining": remaining, "limit": 5000, "resetAt": "x"},
        "search": {
            "issueCount": issue_count,
            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            "nodes": [pr_node(number=n) for n in numbers],
        },
    }


def drive(
    monkeypatch: pytest.MonkeyPatch, state: MiningState, responses: list[dict[str, Any]], **kw: Any
) -> tuple[list[Any], list[dict[str, Any]]]:
    """按顺序喂预设返回，跑完 `mine_repo`，返回 `(每页结果, 每次调用的变量)`。"""
    import cli.mine as mine

    seen_variables: list[dict[str, Any]] = []
    budgets = iter(responses)

    def fake(query: str, variables: Any = None, **kwargs: Any) -> tuple[Any, Any, bool]:
        seen_variables.append(dict(variables or {}))
        data = next(budgets)
        limits = data["rateLimit"]
        budget = gh_cache.RateBudget(
            cost=limits["cost"], remaining=limits["remaining"], limit=limits["limit"]
        )
        return data, budget, False

    monkeypatch.setattr(mine, "cached_graphql", fake)
    monkeypatch.setattr(mine.time, "sleep", lambda _s: None)
    return list(mine.mine_repo(state, **kw)), seen_variables


def one_window() -> MiningState:
    return new_state(
        "pallets/click", since=date(2025, 1, 1), until=date(2025, 1, 31), buckets=1, page_size=25
    )


def test_a_degraded_page_is_retried_instead_of_silently_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub 说这个窗口有 14 条，`nodes` 却是空的 —— 退出码 0、没有 errors 字段。

    2026-09-09 实测撞上过。照收的话这一页会被当成"窗口挖完了"，
    14 个 PR 一条不剩地消失，而报表上只表现为产出率低了一点。
    """
    state = one_window()
    pages, _ = drive(
        monkeypatch,
        state,
        [
            page(issue_count=14, numbers=[]),  # 降级：说有 14 条却什么都没给
            page(issue_count=14, numbers=list(range(1, 15))),  # 重取就正常了
        ],
    )
    assert state.prs_seen == 14, "重取回来的 14 条要算数"
    assert state.problems == []
    assert state.finished is True
    assert len(pages) == 1, "降级那一页不该被当成一页产出交出去"


def test_a_degraded_page_retry_bypasses_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """坏结果已经写进缓存了，重取不绕过的话每次读到的都是同一份空 nodes。"""
    import cli.mine as mine

    refreshes: list[bool] = []

    def fake(query: str, variables: Any = None, **kwargs: Any) -> tuple[Any, Any, bool]:
        refreshes.append(bool(kwargs.get("refresh")))
        data = page(issue_count=14, numbers=[] if len(refreshes) == 1 else [1])
        return data, None, False

    monkeypatch.setattr(mine, "cached_graphql", fake)
    monkeypatch.setattr(mine.time, "sleep", lambda _s: None)
    list(mine.mine_repo(one_window()))

    assert refreshes == [False, True], "第一次照常读缓存，重取时必须绕过"


def test_a_window_that_never_comes_back_is_recorded_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重取到上限还是空的，就承认这个窗口没挖全 —— 写进 problems，报表上看得见。"""
    state = one_window()
    drive(
        monkeypatch,
        state,
        [page(issue_count=14, numbers=[])] * (DEGRADED_RETRIES + 1),
    )
    assert state.finished is True, "不能无限重试，卡住整个作业更糟"
    assert len(state.problems) == 1
    assert "只走到 0 条" in state.problems[0]


def test_a_short_window_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub 说有 10 条，走完只见到 3 条 —— 中间丢页了，而丢页不报错。"""
    state = one_window()
    drive(monkeypatch, state, [page(issue_count=10, numbers=[1, 2, 3])])
    assert len(state.problems) == 1
    assert "实际只走到 3 条" in state.problems[0]


def test_an_empty_window_is_not_mistaken_for_a_degraded_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """这个时间段真的一条都没有。`issueCount` 是 0，不该触发重取。"""
    state = one_window()
    _, variables = drive(monkeypatch, state, [page(issue_count=0, numbers=[])])
    assert len(variables) == 1, "只该问一次"
    assert state.problems == []
    assert state.finished is True


def test_a_window_over_the_search_cap_is_bisected(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub 搜索单个查询最多给 1000 条，超了就必须二分。

    不二分的话，第 1001 条往后的 PR 会被静默丢掉 —— 不报错，只是产出率偏低。
    """
    state = one_window()
    _, variables = drive(
        monkeypatch,
        state,
        [
            page(issue_count=1200, numbers=[]),  # 太宽，这一页作废
            page(issue_count=600, numbers=[1, 2]),  # 前半段
            page(issue_count=600, numbers=[3, 4]),  # 后半段
        ],
    )
    assert len(state.windows) == 2, "原来那个窗口被换成了两半"
    assert state.prs_seen == 4
    # 两个子窗口首尾相接，不重叠
    assert state.windows[0].stop < state.windows[1].start
    assert "merged:2025-01-01..2025-01-16" in variables[1]["q"]
    assert "merged:2025-01-17..2025-01-31" in variables[2]["q"]


def test_paging_follows_the_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    state = one_window()
    _, variables = drive(
        monkeypatch,
        state,
        [
            page(issue_count=30, numbers=list(range(1, 26)), has_next=True, cursor="第二页"),
            page(issue_count=30, numbers=list(range(26, 31))),
        ],
    )
    assert "cursor" not in variables[0], "第一页不带游标"
    assert variables[1]["cursor"] == "第二页"
    assert state.prs_seen == 30
    assert state.pages == 2


def test_running_low_on_quota_stops_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """配额烧到阈值以下就停下来落盘，不硬撑。AC 的"限流下不崩、可续跑"。"""
    state = new_state(
        "pallets/click", since=date(2025, 1, 1), until=date(2025, 6, 30), buckets=3, page_size=25
    )
    pages, variables = drive(
        monkeypatch,
        state,
        [page(issue_count=2, numbers=[1, 2], remaining=100)],
        min_quota=500,
    )
    assert len(variables) == 1, "配额不够就不该再发第二个请求"
    assert state.stopped_reason == "配额不足"
    assert len(pages) == 1, "已经取到的那一页仍然要交出去落库"
    assert len(state.pending()) == 2, "剩下两个窗口留着下次续跑"


def test_max_pages_stops_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    state = new_state(
        "pallets/click", since=date(2025, 1, 1), until=date(2025, 6, 30), buckets=3, page_size=25
    )
    drive(monkeypatch, state, [page(issue_count=1, numbers=[1])] * 2, max_pages=2)
    assert state.stopped_reason == "达到页数上限"
    assert state.pages == 2


def test_non_default_base_branch_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """合并进维护分支（click 的 `stable`）的 PR 照收，但要在报表里看得见。"""
    state = one_window()
    import cli.mine as mine

    data = page(issue_count=1, numbers=[1])
    data["search"]["nodes"][0]["baseRefName"] = "stable"
    monkeypatch.setattr(mine, "cached_graphql", lambda *a, **k: (data, None, False))
    list(mine.mine_repo(state, default_branch="main"))
    assert state.non_default_base_ref == 1


# ══════════════════════════════════════════════════════════════
# 产出率存档：`0 点` 必须可解读
# ══════════════════════════════════════════════════════════════


@pytest.fixture
def report_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import cli.mine as mine

    monkeypatch.setattr(mine, "REPORT_DIR", tmp_path / "mining")
    return tmp_path / "mining"


def test_the_archive_records_whether_the_run_was_served_from_cache(report_dir: Path) -> None:
    """**`points_spent: 0` 有两种完全相反的含义**：全命中缓存所以免费，
    和根本没干活。只记点数的话，这个进版本库的文件里分不出来。

    2026-09-09 真踩到：一次纯缓存重建把"26 个点"覆盖成了 0，
    和 §8.9 里写的数字直接对不上，而文件本身看不出哪个是对的。
    """
    import json

    from cli.mine import write_report

    state = make_state()
    state.windows[0].prs_seen = 100
    state.windows[0].candidates = 30
    cached = gh_cache.CacheStats(hits=14, misses=0)

    path = write_report(state, meta={}, is_domestic=False, cache=cached)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["served_from_cache"] is True
    assert payload["cache"]["hits"] == 14
    assert payload["cache"]["misses"] == 0
    assert payload["state"]["points_spent"] == 0


def test_a_run_that_really_hit_the_network_is_not_marked_cached(report_dir: Path) -> None:
    import json

    from cli.mine import write_report

    state = make_state()
    state.points_spent = 26
    path = write_report(
        state, meta={}, is_domestic=False, cache=gh_cache.CacheStats(hits=1, misses=13)
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["served_from_cache"] is False
    assert payload["state"]["points_spent"] == 26


def test_an_archive_written_without_cache_stats_is_not_claimed_to_be_cached(
    report_dir: Path,
) -> None:
    """拿不到缓存统计时宁可说"不是缓存"——把没干活的一轮标成"免费"更误导。"""
    import json

    from cli.mine import write_report

    path = write_report(make_state(), meta={}, is_domestic=False)
    assert json.loads(path.read_text(encoding="utf-8"))["served_from_cache"] is False
