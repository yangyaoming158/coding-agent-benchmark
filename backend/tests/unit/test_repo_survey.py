"""仓库选型的度量与打分（E8-T1，`03-benchmark-spec.md` §8.3）。

这一组**不联网、不用 Docker**。真正花时间的两件事（打 GitHub API、进容器装依赖）
是薄薄一层胶水，靠不住的是它们两头的判断：中文怎么算、哪个 PR 算候选、
哪条门槛没过。那些都是纯函数，在这里全测掉。

## 为什么中文判定值得这么多用例

"中文 issue 比例"是本项目的公开指标（§8.5）。判偏了不会报错，只会让选型依据
和最终报告里的数字一起偏 —— 而且偏的方向是系统性的：中文 issue 里贴英文报错日志
是常态，不剥代码块的话，好 issue 反而更容易被判成英文。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.benchmark.survey import (
    MAX_INSTALL_S,
    MIN_CANDIDATE_PRS,
    MIN_PYTHON_RATIO,
    RepoFacts,
    cjk_ratio,
    count_issue_languages,
    count_pr_candidates,
    detect_language,
    evaluate,
    pr_is_candidate,
    python_ratio,
    rank,
    strip_noise,
    touches_source_code,
    touches_test_code,
)
from app.domain.enums import IssueLanguage
from cli.survey import _window_buckets, load_candidates

# ══════════════════════════════════════════════════════════════
# 中文判定
# ══════════════════════════════════════════════════════════════

#: 一条典型的中文 issue：正文是中文，中间贴了一大段英文 traceback。
#: 这是最容易判错的形状，而它恰恰是**信息量最足**的那种 issue。
ZH_ISSUE_WITH_LOG = """解析带引号的 CSV 行时字段被切断了

用 `parse_line` 处理下面这一行的时候，引号里的逗号被当成了分隔符：

```python
Traceback (most recent call last):
  File "/usr/lib/python3.11/site-packages/textkit/csvline.py", line 42, in parse_line
    return line.rstrip("\\n").split(",")
AssertionError: assert ['a', 'b', 'c'] == ['a,b', 'c']
```

期望得到两个字段，实际得到三个。相关文档见 https://example.com/docs/csv 。
"""

EN_ISSUE = """Quoted commas are treated as separators

When calling `parse_line` on a row that contains a quoted comma, the parser
splits inside the quotes instead of keeping the field together. This breaks
our log import pipeline because every row after the first bad one is shifted.
"""


def test_chinese_issue_with_english_traceback_is_zh() -> None:
    """中文 issue 里贴英文日志，不能被判成英文。

    这一条是整个中文比例指标的成败所在：不剥代码块的话，
    traceback 里几十个英文词会把比例压到阈值以下。
    """
    assert detect_language(ZH_ISSUE_WITH_LOG) is IssueLanguage.ZH


def test_english_issue_is_en() -> None:
    assert detect_language(EN_ISSUE) is IssueLanguage.EN


def test_mostly_english_with_a_chinese_sentence_is_mixed() -> None:
    """英文为主、夹一句中文 → `mixed`。

    `mixed` 是一条窄带（中文占 5%~30%）：再多就是中文 issue，再少就是
    英文 issue 里带了个中文变量名。带宽窄是刻意的 —— 这个值会进数据集统计页，
    含义得清楚。
    """
    text = (
        "The upstream service seems to time out under load. "
        "We see intermittent five hundred responses from the login endpoint "
        "whenever the connection pool is saturated during peak hours. "
        "登录接口偶尔返回 500。"
    )
    assert detect_language(text) is IssueLanguage.MIXED


def test_too_short_falls_back_to_en() -> None:
    """判不出来时按英文算 —— 中文比例只会低估不会高估。

    拿这个数当卖点，宁可保守：报出来的比例比真实低，没人会说我们注水。
    """
    assert detect_language("不工作") is IssueLanguage.EN
    assert detect_language("") is IssueLanguage.EN


def test_all_code_falls_back_to_en() -> None:
    """整条 issue 只有一段代码，剥完就没内容了。"""
    assert detect_language("```\nimport os\nprint(os.getcwd())\n```") is IssueLanguage.EN


def test_strip_noise_removes_code_urls_and_tags() -> None:
    text = "看这里 `parse_line` 和 https://example.com/x 还有 <b>标签</b>\n```\ncode\n```"
    cleaned = strip_noise(text)
    assert "parse_line" not in cleaned
    assert "example.com" not in cleaned
    assert "code" not in cleaned
    assert "看这里" in cleaned


def test_cjk_ratio_counts_latin_by_word_not_by_letter() -> None:
    """分母用英文**词**数不是字母数。

    按字母数算的话，一句英文的分母是一句中文的四五倍，
    "一句中文 + 一句英文"会被系统性地判成英文。
    """
    text = "这句话有八个字 plus four english words"
    ratio = cjk_ratio(text)
    # 7 个汉字 vs 4 个英文词 → 明显偏中文；按字母数算的话是 7/(7+24)≈0.23，会翻车
    assert ratio > 0.5


# ══════════════════════════════════════════════════════════════
# 哪个 PR 算候选
# ══════════════════════════════════════════════════════════════


def test_test_code_detection_uses_the_shared_pattern_list() -> None:
    assert touches_test_code(["tests/test_a.py"])
    assert touches_test_code(["src/pkg/tests/test_b.py"])
    assert touches_test_code(["src/foo_test.py"])
    assert not touches_test_code(["src/foo.py", "README.md"])


def test_pyproject_alone_is_not_a_test_change() -> None:
    """只改 `pyproject.toml` 不算"带了测试改动"。

    它在**受保护**清单里（改了能改变测试行为），但它不装用例。
    拿整份受保护清单判的话，候选池深度会虚高 —— 而那个数字是选型依据。
    """
    assert not touches_test_code(["pyproject.toml"])
    assert not touches_test_code(["setup.cfg", ".github/workflows/ci.yml"])


def test_source_code_detection_excludes_all_protected_paths() -> None:
    assert touches_source_code(["src/foo.py"])
    assert not touches_source_code(["tests/test_a.py", "pyproject.toml"])


@pytest.mark.parametrize(
    ("paths", "linked", "expected"),
    [
        (["src/a.py", "tests/test_a.py"], 1, True),
        # 没关联 issue → 没有题面
        (["src/a.py", "tests/test_a.py"], 0, False),
        # 没测试改动 → 抽不出 F2P
        (["src/a.py", "README.md"], 1, False),
        # 只改测试 → §7.2(7)"任务只需改测试即可通过"，明令丢弃
        (["tests/test_a.py"], 1, False),
        # 只改配置 → 两头都不占
        (["pyproject.toml"], 1, False),
    ],
)
def test_pr_is_candidate(paths: list[str], linked: int, expected: bool) -> None:
    assert pr_is_candidate(paths=paths, linked_issues=linked) is expected


# ══════════════════════════════════════════════════════════════
# GraphQL 返回的解析
# ══════════════════════════════════════════════════════════════


def test_count_pr_candidates_handles_nulls() -> None:
    """GraphQL 里 `files` 可能是 null，`nodes` 里可能混进空对象。

    漏判一个 None 会在跑到第 13 个仓库时崩掉，而那时前面的配额已经烧掉了。
    """
    nodes = [
        {
            "number": 1,
            "closingIssuesReferences": {"totalCount": 1},
            "files": {"nodes": [{"path": "src/a.py"}, {"path": "tests/test_a.py"}]},
        },
        # PR 太大，GitHub 不给展开文件列表 → 计入"查过"但不算候选
        {"number": 2, "closingIssuesReferences": {"totalCount": 1}, "files": None},
        # search 返回里混进来的非 PR 节点
        {},
        None,
    ]
    examined, candidates = count_pr_candidates(nodes)  # type: ignore[arg-type]
    assert (examined, candidates) == (2, 1)


def test_broad_mode_counts_mention_style_links() -> None:
    """宽口径把正文里的 `#123` 也算成关联 issue。

    中文项目普遍写"修复 #123"而不是 `fixes #123`，只有后者会被 GitHub 记进
    `linked:issue`。这一列不参与门槛，只用来回答"放宽之后候选池能大多少"。
    """
    node = {
        "number": 7,
        "bodyText": "修复 #123，顺手补了一条回归测试",
        "closingIssuesReferences": {"totalCount": 0},
        "files": {"nodes": [{"path": "src/a.py"}, {"path": "tests/test_a.py"}]},
    }
    assert count_pr_candidates([node]) == (1, 0)  # type: ignore[arg-type]
    assert count_pr_candidates([node], allow_mention=True) == (1, 1)  # type: ignore[arg-type]


def test_broad_mode_still_needs_test_and_source_changes() -> None:
    """放宽的只是"怎么算关联 issue"，另外两条过滤一条都不放。"""
    node = {
        "number": 8,
        "bodyText": "修复 #123",
        "closingIssuesReferences": {"totalCount": 0},
        "files": {"nodes": [{"path": "README.md"}]},
    }
    assert count_pr_candidates([node], allow_mention=True) == (1, 0)  # type: ignore[arg-type]


def test_broad_estimate_uses_all_merged_prs_as_base() -> None:
    """宽口径的基数是**全部** merged PR，因为它的抽样就是从那里取的。"""
    facts = RepoFacts(
        full_name="owner/repo",
        merged_prs_2y=600,
        linked_prs_2y=100,
        prs_examined=50,
        prs_candidate=25,
        prs_examined_broad=60,
        prs_candidate_broad=12,
    )
    assert facts.estimated_candidates == 50  # 严口径：100 × 0.5
    assert facts.estimated_candidates_broad == 120  # 宽口径：600 × 0.2


def test_count_issue_languages_uses_title_and_body() -> None:
    """标题中文、正文全是英文日志的 issue 要算中文。

    不少中文项目就是这个写法，只看正文会把它判成英文。
    """
    nodes = [
        {"title": "解析带引号的 CSV 行时字段被切断了", "body": ZH_ISSUE_WITH_LOG},
        {"title": "Quoted commas are treated as separators", "body": EN_ISSUE},
        # body 为 null 是常见情况（只有标题的 issue）
        {"title": "打包之后中文文件名乱码，求修复", "body": None},
        None,
    ]
    sampled, zh, mixed = count_issue_languages(nodes)  # type: ignore[arg-type]
    assert sampled == 3
    assert zh == 2
    assert mixed == 0


def test_python_ratio() -> None:
    assert python_ratio({"Python": 900, "SQL": 100}) == 0.9
    assert python_ratio({}) == 0.0


# ══════════════════════════════════════════════════════════════
# 门槛判定
# ══════════════════════════════════════════════════════════════


def healthy_facts(**overrides: object) -> RepoFacts:
    facts = RepoFacts(
        full_name="owner/repo",
        group="测试",
        license_spdx="MIT",
        archived=False,
        pushed_at="2026-09-01T00:00:00Z",
        python_ratio=0.95,
        merged_prs_2y=400,
        linked_prs_2y=300,
        prs_examined=100,
        prs_candidate=50,
        issues_sampled=100,
        issues_zh=80,
        issues_mixed=10,
        install_s=40.0,
        test_s=60.0,
    )
    for key, value in overrides.items():
        setattr(facts, key, value)
    return facts


def test_python_ratio_gate_keeps_dominant_python_repos() -> None:
    """按字节占比卡 80% 会误杀纯 Python 项目，所以门槛定在 50%。

    实测被误杀的三个：sqlfluff 71%（测试语料是 .sql）、nonebot2 63%（仓库带官网）、
    jieba 52%（linguist 把词典误判成 OpenEdge ABL）。50% 同时保证了
    "Python 是占比第一的语言"，真正多语言的仓库照样进不来。
    """
    assert MIN_PYTHON_RATIO == 0.50
    for ratio in (0.71, 0.63, 0.52):
        assert not evaluate(healthy_facts(python_ratio=ratio)).failed
    # mmcv 那种 39% 的仍然出局
    assert [g.name for g in evaluate(healthy_facts(python_ratio=0.39)).failed] == ["Python 占比"]


def test_healthy_repo_passes_every_gate() -> None:
    verdict = evaluate(healthy_facts())
    assert verdict.passed
    assert verdict.label == "入选"
    assert not verdict.failed


def test_unmeasured_is_not_treated_as_passing() -> None:
    """没测到的门槛既不算过也不算不过，但整体**不算入选**。

    反过来（没测到就当过）会让一个只跑了 API 段的仓库看起来已经合格。
    """
    verdict = evaluate(healthy_facts(install_s=None, test_s=None))
    assert not verdict.passed
    assert not verdict.failed
    assert verdict.label == "待测"
    assert {g.name for g in verdict.unknown} == {"安装耗时", "测试耗时"}


@pytest.mark.parametrize(
    ("field", "value", "gate"),
    [
        ("python_ratio", 0.39, "Python 占比"),
        ("license_spdx", "GPL-3.0", "许可证"),
        ("archived", True, "仓库活跃"),
        ("pushed_at", "2024-01-01T00:00:00Z", "仓库活跃"),
        ("install_s", 300.0, "安装耗时"),
        ("test_s", 400.0, "测试耗时"),
    ],
)
def test_each_gate_can_fail_on_its_own(field: str, value: object, gate: str) -> None:
    verdict = evaluate(healthy_facts(**{field: value}))
    assert [g.name for g in verdict.failed] == [gate]


def test_candidate_depth_uses_linked_count_as_the_base() -> None:
    """候选池深度 = 关联 issue 的 PR 数（精确）× 抽样合格率。

    基数用 `linked_prs_2y` 而不是 `merged_prs_2y`：抽样本身就是从
    `linked:issue` 那批里取的，拿全量 merged 当基数会把比例套错人群。
    """
    facts = healthy_facts(merged_prs_2y=1000, linked_prs_2y=200, prs_examined=100, prs_candidate=50)
    assert facts.estimated_candidates == 100  # 200 × 0.5，不是 1000 × 0.5
    assert evaluate(facts).passed

    thin = healthy_facts(linked_prs_2y=20, prs_examined=100, prs_candidate=50)
    assert thin.estimated_candidates == 10
    assert [g.name for g in evaluate(thin).failed] == ["候选池深度"]
    assert MIN_CANDIDATE_PRS == 15


def test_zh_ratio_counts_mixed_as_half() -> None:
    facts = healthy_facts(issues_sampled=10, issues_zh=4, issues_mixed=2)
    assert facts.zh_ratio == pytest.approx(0.5)


def test_rank_puts_passing_repos_first_then_by_chinese_ratio() -> None:
    chosen_zh = evaluate(healthy_facts(issues_zh=90, issues_mixed=0))
    chosen_en = evaluate(healthy_facts(issues_zh=0, issues_mixed=0))
    unmeasured = evaluate(healthy_facts(install_s=None))
    rejected = evaluate(healthy_facts(license_spdx="GPL-3.0"))

    order = rank([rejected, chosen_en, unmeasured, chosen_zh])
    assert [v.label for v in order] == ["入选", "入选", "待测", "出局"]
    assert order[0].facts.zh_ratio == 0.9


def test_facts_survive_a_json_round_trip() -> None:
    """两段分开跑，中间要落盘，所以序列化必须无损。"""
    facts = healthy_facts(problems=["clone 失败"])
    restored = RepoFacts.from_json(facts.to_json())
    assert restored == facts
    # 派生量也写进 JSON，报表和后续分析直接读
    assert facts.to_json()["estimated_candidates"] == facts.estimated_candidates
    assert MAX_INSTALL_S == 120


# ══════════════════════════════════════════════════════════════
# GitHub 客户端的失败处理
# ══════════════════════════════════════════════════════════════


def test_gh_timeout_becomes_a_github_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """`gh` 超时要包成 `GitHubError`，不能让 `TimeoutExpired` 冒出去。

    调用方（`cli.survey.probe_repo`）只 catch `GitHubError`。漏一种异常类型
    就等于"第 13 个仓库慢了一次，前 12 个的结果连同烧掉的配额一起没了"——
    2026-09-08 探 PaddleOCR 时真这么翻过一次。
    """
    import subprocess

    from app.benchmark import github

    def always_timeout(*_args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="gh", timeout=1)

    monkeypatch.setattr(github.subprocess, "run", always_timeout)
    monkeypatch.setattr(github.time, "sleep", lambda _s: None)

    with pytest.raises(github.GitHubError, match="没返回"):
        github.rest("repos/owner/repo", timeout_s=1)


def test_missing_repo_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """404 是结论不是故障，不该白白退避重试四次。"""
    import subprocess

    from app.benchmark import github

    calls = {"n": 0}

    def not_found(*_args: object, **_kwargs: object) -> object:
        calls["n"] += 1
        return subprocess.CompletedProcess(
            args=["gh"], returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)"
        )

    monkeypatch.setattr(github.subprocess, "run", not_found)
    with pytest.raises(github.GitHubNotFoundError):
        github.rest("repos/owner/nope")
    assert calls["n"] == 1


# ══════════════════════════════════════════════════════════════
# 候选名单与抽样窗口
# ══════════════════════════════════════════════════════════════


def test_load_candidates_keeps_groups_and_skips_comments(tmp_path: Path) -> None:
    path = tmp_path / "candidates.txt"
    path.write_text(
        "# 注释\n\n[国产]\nowner/a\nowner/b\n\n[对照]\n# 又一条注释\nowner/c\n",
        encoding="utf-8",
    )
    assert load_candidates(path) == [
        ("owner/a", "国产"),
        ("owner/b", "国产"),
        ("owner/c", "对照"),
    ]


def test_window_buckets_cover_the_whole_window_without_gaps() -> None:
    """分桶要铺满近 2 年且首尾相接 —— 漏掉一段就是漏掉那一段的 PR。"""
    buckets = _window_buckets(6)
    assert len(buckets) == 6
    # 桶是从今天往回排的：第 0 个最近，最后一个最早
    for i in range(len(buckets) - 1):
        assert buckets[i][0] == buckets[i + 1][1], "相邻两段必须首尾相接，中间不能漏"
    assert buckets[-1][0] < buckets[0][1], "整体要铺满整个窗口"


def test_real_candidate_file_has_enough_domestic_repos() -> None:
    """§8.3 要求"国产/中文社区至少 4–6 个仓库"，候选池要留出淘汰余地。"""
    pairs = load_candidates()
    assert len(pairs) >= 15
    domestic = [name for name, group in pairs if "国产" in group or "中文" in group]
    assert len(domestic) >= 6
