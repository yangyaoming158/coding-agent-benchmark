"""候选 → 题目的组装（E8-T2，`03-benchmark-spec.md` §7.1/§7.2/§7.7/§7.8）。

**这一组不起容器、不联网。** 组装拿到的输入是两份"用例 ID → 状态"的表，
那正是全量报告解析完的样子，所以判断部分全是纯函数，在这里测掉。

四处最值得测，因为**错了不会报错，只会让题库悄悄变差**：

1. **F2P 展开。** 候选是基名（`test_x`），报告里是参数化变体（`test_x[a]`）。
   不展开就一律判 MISSING，好题被当成坏题丢掉 —— 2026-09-10 实测 click 的
   main 分支五条候选全军覆没就是这个原因。
2. **P2P 必须取两轮的交集。** 只用基线那一半的话，gold 顺带改了行为的用例会在
   验证流水线的 S8 被记成 `GOLD_REGRESSION`，整道好题被丢，而且理由是错的。
3. **喂不回给 pytest 的用例 ID 要滤掉。** 混进去一条，这道题以后每次评测都
   颗粒无收（pytest 报 usage error，一条用例都不跑），表现却是"报告是空的"。
4. **难度分级数的是改动行，不是 diff 行。** 文件头那两行也以 `+++` / `---` 开头，
   按行首字符数的话每个文件白送两行，小题会被算成大题。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.benchmark.assembly import (
    FULL_SUITE_BUDGET_S,
    RANDOM_SAMPLE_SIZE,
    AssemblyError,
    Candidate,
    assemble,
    derive_difficulty,
    environment_from_recipe,
    expand_candidate,
    function_name_of,
    is_flaky,
    load_candidate,
    patch_size,
    round_trippable,
    same_module_cases,
    select_f2p,
    select_p2p,
)
from app.benchmark.schema import P2PSampling
from app.domain.enums import IssueLanguage, TaskDifficulty, TaskValidationState
from app.sandbox.images import parse_recipe
from cli.promote import next_state

# ══════════════════════════════════════════════════════════════
# F2P 展开与证伪（§7.2(5)）
# ══════════════════════════════════════════════════════════════


def test_expand_candidate_finds_parametrized_variants() -> None:
    """基名要能找到它的参数化变体。

    §8.10 第四节当时写的是"参数化用例只给基名，靠 E4 的用例 ID 归一化去对"，
    但 `ParsedReport.resolve()` 的三层匹配里没有这一层。不在这儿展开，
    带参数的候选一律判 `MISSING`，题目被误判成 `F2P_NOT_FAILING`。
    """
    cases = [
        "tests/test_options.py::test_usage_show_choices[text choices]",
        "tests/test_options.py::test_usage_show_choices[int choices]",
        "tests/test_options.py::test_other",
    ]
    got = expand_candidate("tests/test_options.py::test_usage_show_choices", cases)
    assert got == sorted(cases[:2]), "展开结果排过序，方便和题目里的 F2P 逐条比对"


def test_expand_candidate_does_not_match_a_longer_name() -> None:
    """`test_foo` 不能顺手把 `test_foobar` 也收进来 —— 前缀判据必须带上 `[`。"""
    cases = ["tests/t.py::test_foobar", "tests/t.py::test_foo[1]"]
    assert expand_candidate("tests/t.py::test_foo", cases) == ["tests/t.py::test_foo[1]"]


def test_select_f2p_keeps_only_variants_that_fail_then_pass() -> None:
    """§7.2(5) 的两条断言：基线上失败、打完 gold 通过。逐条判，不是"至少一条"。"""
    base = {
        "t.py::test_x[a]": "FAILED",
        "t.py::test_x[b]": "PASSED",  # 基线上就通过 → 不揭示 bug
        "t.py::test_x[c]": "ERROR",
    }
    gold = {"t.py::test_x[a]": "PASSED", "t.py::test_x[b]": "PASSED", "t.py::test_x[c]": "PASSED"}

    got = select_f2p(candidates=["t.py::test_x"], baseline_status=base, gold_status=gold)

    assert got.ids == ("t.py::test_x[a]", "t.py::test_x[c]")
    assert got.not_failing == {"t.py::test_x[b]": "PASSED"}


def test_select_f2p_drops_cases_gold_does_not_fix() -> None:
    """基线挂了、打完 gold 还挂 —— 它在这个环境里本来就是坏的，和这道题没关系。

    进 F2P 的话，验证流水线的 S7 会判 `GOLD_NOT_FIXING`，整道题被丢掉。
    """
    base = {"t.py::test_x[a]": "FAILED", "t.py::test_x[b]": "FAILED"}
    gold = {"t.py::test_x[a]": "PASSED", "t.py::test_x[b]": "FAILED"}

    got = select_f2p(candidates=["t.py::test_x"], baseline_status=base, gold_status=gold)

    assert got.ids == ("t.py::test_x[a]",)
    assert got.not_fixed == ("t.py::test_x[b]",)


def test_select_f2p_separates_uncollectable_from_truly_absent() -> None:
    """ "base 上收集不出来"和"两边都没有"是两回事，混在一起就查不出题库为什么变小。

    前者的典型形状是新测试 import 了 gold 才加的符号（实测 click #3637 就是）。
    它当不了 F2P（协议 C-12 不认 MISSING 是失败），但丢掉的原因得说得清。
    """
    got = select_f2p(
        candidates=["t.py::test_new", "t.py::test_never"],
        baseline_status={"t.py::test_other": "PASSED"},
        gold_status={"t.py::test_new": "PASSED", "t.py::test_other": "PASSED"},
    )

    assert got.ids == ()
    assert got.uncollectable_on_base == ("t.py::test_new",)
    assert got.unmatched == ("t.py::test_never",)


def test_select_f2p_drops_ids_pytest_cannot_take_back() -> None:
    """非 ASCII 参数的 ID 进了 F2P，这道题每次评测都会颗粒无收。见 `round_trippable`。"""
    base = {"t.py::test_x[字]": "FAILED", "t.py::test_x[ok]": "FAILED"}
    gold = {"t.py::test_x[字]": "PASSED", "t.py::test_x[ok]": "PASSED"}

    got = select_f2p(candidates=["t.py::test_x"], baseline_status=base, gold_status=gold)

    assert got.ids == ("t.py::test_x[ok]",)
    assert got.dropped_unusable == ("t.py::test_x[字]",)


# ══════════════════════════════════════════════════════════════
# 用例 ID 能不能喂回给 pytest
# ══════════════════════════════════════════════════════════════


def test_round_trippable_rejects_non_ascii_parameters() -> None:
    """2026-09-10 实测：pytest 生成参数化 ID 时把非 ASCII 转义成 `\\u5b57` 这种文本，
    写 junit 时又写回真正的字。从报告里读出来的 ID 喂回去，pytest 报
    `ERROR: not found` —— 而且是 usage error（退出码 4），**整轮一条用例都不跑**。
    """
    assert round_trippable("tests/test_termui.py::test_getchar_windows[True-x]")
    assert not round_trippable("tests/test_termui.py::test_getchar_windows[True-字]")


def test_round_trippable_keeps_ascii_escapes() -> None:
    """`\\x1b[31mred` 这种是**纯 ASCII 的转义文本**，pytest 两边写法一致，要留着。

    误杀它的话 click 那批 ANSI 用例会整批退出回归护栏。
    """
    assert round_trippable("tests/test_compat.py::test_strip_ansi[\\x1b[31mred-red]")


# ══════════════════════════════════════════════════════════════
# P2P 派生（§7.2(6) + §7.7）
# ══════════════════════════════════════════════════════════════


def test_select_p2p_takes_the_intersection_of_both_rounds() -> None:
    """P2P = 基线通过 **∩** 打完 gold 仍然通过（§7.2(6)）。

    只用基线那一半的话，`only_base` 会进 P2P，然后在验证流水线的 S8 被记成
    `GOLD_REGRESSION` —— 好题被丢掉，而且诊断是错的：gold 没有回归，
    是我们把不该当护栏的用例塞进了护栏。
    """
    got = select_p2p(
        baseline_passing=["t.py::a", "t.py::only_base"],
        gold_passing=["t.py::a", "t.py::only_gold"],
        fail_to_pass=(),
        suite_seconds=1.0,
        gold_patch=_diff("src/x.py", added=1),
    )

    assert got.ids == ("t.py::a",)
    assert got.lost_after_gold == ("t.py::only_base",)


def test_select_p2p_never_overlaps_fail_to_pass() -> None:
    """F2P 和 P2P 有交集的话 `TaskDefinition` 直接拒收 —— 一条用例不能同时
    "修好之前必须失败"和"修好之前就该通过"。
    """
    got = select_p2p(
        baseline_passing=["t.py::a", "t.py::f"],
        gold_passing=["t.py::a", "t.py::f"],
        fail_to_pass=["t.py::f"],
        suite_seconds=1.0,
        gold_patch=_diff("src/x.py", added=1),
    )
    assert got.ids == ("t.py::a",)


def test_select_p2p_uses_full_strategy_for_a_fast_suite() -> None:
    """§7.7 第一条：套件 ≤ 3 分钟就不抽样，护栏越全越好。"""
    got = select_p2p(
        baseline_passing=[f"t.py::t{i}" for i in range(500)],
        gold_passing=[f"t.py::t{i}" for i in range(500)],
        fail_to_pass=(),
        suite_seconds=FULL_SUITE_BUDGET_S,
        gold_patch=_diff("src/x.py", added=1),
    )
    assert got.sampling.strategy == "full"
    assert got.sampling.seed is None
    assert len(got.ids) == got.sampling.total_pool == 500


def test_select_p2p_samples_a_slow_suite_and_keeps_the_same_module() -> None:
    """套件太慢就抽样，但**同模块的用例一条不落**（§7.7 第二条）——
    gold 改了哪个文件，回归风险最高的就是那个文件的测试。
    """
    cases = [f"tests/test_types.py::t{i}" for i in range(5)]
    cases += [f"tests/test_other.py::t{i}" for i in range(1000)]
    got = select_p2p(
        baseline_passing=cases,
        gold_passing=cases,
        fail_to_pass=(),
        suite_seconds=FULL_SUITE_BUDGET_S + 1,
        gold_patch=_diff("src/click/types.py", added=1),
    )

    assert got.sampling.strategy == "module_and_random"
    assert got.sampling.seed is not None, "没有种子就复现不出当初选了哪些"
    assert set(cases[:5]) <= set(got.ids), "同模块的必须全在"
    assert len(got.ids) == 5 + RANDOM_SAMPLE_SIZE


def test_select_p2p_sampling_is_reproducible() -> None:
    """同一个种子必须选出同一批。抽样参数决定 P2P 名单，P2P 名单决定判定结论 ——
    换一批就等于"同一个数据集版本"给不出同样的判定（§7.9）。
    """
    cases = [f"tests/test_other.py::t{i}" for i in range(1000)]
    kwargs = {
        "baseline_passing": cases,
        "gold_passing": cases,
        "fail_to_pass": (),
        "suite_seconds": FULL_SUITE_BUDGET_S + 1,
        "gold_patch": _diff("src/click/types.py", added=1),
    }
    assert select_p2p(**kwargs).ids == select_p2p(**kwargs).ids  # type: ignore[arg-type]


def test_same_module_cases_matches_on_the_file_stem() -> None:
    cases = ["tests/test_types/test_Path.py::a", "tests/test_termui.py::b"]
    assert same_module_cases(cases, _diff("src/click/types.py", added=1)) == {cases[0]}


# ══════════════════════════════════════════════════════════════
# 难度（§7.8）
# ══════════════════════════════════════════════════════════════


def _diff(path: str, *, added: int = 0, removed: int = 0) -> str:
    """造一份最小的 unified diff。行数和文件数是难度分级的全部输入。"""
    body = "".join(f"+line {i}\n" for i in range(added))
    body += "".join(f"-line {i}\n" for i in range(removed))
    return (
        f"diff --git a/{path} b/{path}\n"
        f"index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,{max(removed, 1)} +1,{max(added, 1)} @@\n"
        f"{body}"
    )


def test_patch_size_does_not_count_the_file_header_lines() -> None:
    """`--- a/x.py` 和 `+++ b/x.py` 也以 `-` / `+` 开头。

    按行首字符数的话每个文件白送两行，`easy`（≤15 行）的题会被算成 `medium`。
    难度错了不会报错，只会让难度分布这张图是假的。
    """
    assert patch_size(_diff("src/x.py", added=3, removed=2)) == (1, 5)


@pytest.mark.parametrize(
    ("files", "lines", "expected"),
    [
        (1, 10, TaskDifficulty.EASY),
        (1, 16, TaskDifficulty.MEDIUM),  # 文件数够 easy 但行数超了
        (3, 60, TaskDifficulty.MEDIUM),
        (4, 10, TaskDifficulty.HARD),  # 行数很少但摊在 4 个文件上
        (2, 61, TaskDifficulty.HARD),
    ],
)
def test_difficulty_follows_the_two_thresholds(
    files: int, lines: int, expected: TaskDifficulty
) -> None:
    """§7.8：easy ≤1 文件且 ≤15 行；medium ≤3 文件且 ≤60 行；其余 hard。"""
    per_file = lines // files
    patch = "".join(
        _diff(f"src/x{i}.py", added=per_file + (lines % files if i == 0 else 0))
        for i in range(files)
    )
    assert derive_difficulty(patch) == expected


# ══════════════════════════════════════════════════════════════
# 环境规格
# ══════════════════════════════════════════════════════════════


RECIPE = {
    "environment_id": "pallets__click__py311",
    "repo_name": "pallets/click",
    "repo_url": "https://github.com/pallets/click.git",
    "snapshot_commit": "6aabf099bfdd4c1e75fe8d0e0d4241372b988ab1",
    "install_steps": ["python -m pip install -e ."],
    "import_check": ["click"],
}


def test_environment_command_meets_the_hard_requirements() -> None:
    """§7.2(4)：机器可解析的逐用例报告、禁随机顺序与缓存、**不能有 `-x`**。

    `-x` 会让套件在第一条失败处停下，我们就拿不到全部用例状态 ——
    而 P2P 候选池要的正是"全部通过的用例"。
    """
    env = environment_from_recipe(parse_recipe(RECIPE))
    assert "--junitxml=" in env.test_command
    assert "-p no:randomly" in env.test_command
    assert "-p no:cacheprovider" in env.test_command
    assert " -x" not in env.test_command
    assert "--continue-on-collection-errors" in env.test_command
    assert "--timeout=" in env.test_command


def test_report_path_is_relative_to_the_workspace() -> None:
    """执行器读的是 `workspace.path / test_report_path`。

    写成 `/tmp/report.xml` 的话 `Path` 的拼接规则会让它变成**宿主机**的
    `/tmp/report.xml` —— 读到的是上一次的报告，或者干脆读不到，两种都不报错。
    """
    env = environment_from_recipe(parse_recipe(RECIPE))
    assert not Path(env.test_report_path).is_absolute()


def test_environment_appends_the_repos_own_test_args() -> None:
    """`test_args` 照抄仓库自己的测试命令（E2-T3 留给本任务的字段）。"""
    env = environment_from_recipe(
        parse_recipe({**RECIPE, "test_args": ["--import-mode=importlib"]})
    )
    assert env.test_command.endswith("--import-mode=importlib")


# ══════════════════════════════════════════════════════════════
# 读候选
# ══════════════════════════════════════════════════════════════


def test_load_candidate_says_which_patch_file_is_missing(tmp_path: Path) -> None:
    """补丁落在磁盘上、不在 `raw_payload` 里（§8.10 第三节）。

    缺文件要当场报错并指出路径 —— 缺补丁的候选组装不出题目，
    早报比"组装出一个 gold_patch 为空的题"好查得多。
    """
    payload = {
        "repo": "pallets/click",
        "pr": {"number": 42},
        "base_commit": "a" * 40,
        "cleaned": {"issue_title": "t", "issue_body": "b", "f2p_candidates": ["t.py::x"]},
    }
    with pytest.raises(AssemblyError, match="补丁文件不在"):
        load_candidate(payload, patch_root=tmp_path)


def test_load_candidate_rejects_an_uncleaned_payload(tmp_path: Path) -> None:
    """没洗过的候选题面还带着泄题，不能直接拿来建题。"""
    with pytest.raises(AssemblyError, match="还没清洗过"):
        load_candidate({"repo": "a/b", "pr": {"number": 1}}, patch_root=tmp_path)


# ══════════════════════════════════════════════════════════════
# 人工终审之后的状态迁移（§7.4）
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("current", "verdict", "expected"),
    [
        # 收下一道带复核旗子的题 —— **这条边第一版漏了**
        (TaskValidationState.REVIEW_REQUIRED, "ACCEPT", TaskValidationState.VALID),
        (TaskValidationState.REVIEW_REQUIRED, "REJECT", TaskValidationState.INVALID),
        (TaskValidationState.VALID, "ACCEPT", TaskValidationState.VALID),
        (TaskValidationState.VALID, "REJECT", TaskValidationState.INVALID),
    ],
)
def test_human_verdict_moves_the_task_along_the_state_machine(
    current: TaskValidationState, verdict: str, expected: TaskValidationState
) -> None:
    """§7.4 写的是 `REVIEW_REQUIRED →（人工）→ VALID / INVALID`，**两条边都要走**。

    漏掉 `ACCEPT` 那一条的后果是静默的：被人看过并且收下的题永远卡在
    `REVIEW_REQUIRED`，而 E1-T6 按 `VALID` 挑题，它会掉出数据集而且不报错。
    2026-09-10 真这么错过一次：收下 21 条，库里只有 20 道 VALID。
    """
    assert next_state(current, verdict) is expected


# ── 已知不稳定用例不许进 P2P（E1-T6 补）─────────────────────


def test_function_name_of_strips_params_and_class() -> None:
    assert function_name_of("tests/a.py::test_y") == "test_y"
    assert function_name_of("tests/a.py::TestX::test_y") == "test_y"
    assert function_name_of("tests/a.py::test_y[p1-p2]") == "test_y"
    assert function_name_of("tests/a.py::TestX::test_y[less]") == "test_y"


def test_is_flaky_matches_the_whole_family() -> None:
    """飘的是**这个函数**，不是某一组参数 —— 整族剔。"""
    assert is_flaky("tests/test_utils.py::test_echo_via_pager[test5-less]")
    assert is_flaky("tests/test_utils.py::test_echo_via_pager[test6-cat ]")
    assert is_flaky("tests/test_utils.py::test_echo_via_pager")


def test_is_flaky_does_not_match_by_substring() -> None:
    """**同名前缀的别的函数不能被误伤。**

    click 里有 8 个函数名里带 `echo_via_pager`，只有那一个参数化家族飘过。
    按子串匹配的话会白白丢掉 47 条好护栏。
    """
    for case in (
        "tests/test_termui.py::test_echo_via_pager_streams_each_write",
        "tests/test_utils.py::test_echo_via_pager_yields_before_exception",
        "tests/test_testing.py::test_with_echo_via_pager",
        "tests/test_termui.py::test_tempfile_pager_accepts_text[echo_via_pager]",
    ):
        assert not is_flaky(case), case


def test_select_p2p_drops_flaky_cases() -> None:
    cases = [
        "tests/test_a.py::test_ok",
        "tests/test_utils.py::test_echo_via_pager[test5-less]",
        "tests/test_utils.py::test_echo_via_pager[test6-cat]",
    ]
    result = select_p2p(
        baseline_passing=cases,
        gold_passing=cases,
        fail_to_pass=[],
        suite_seconds=1.0,
        gold_patch="diff --git a/src/x.py b/src/x.py\n",
    )
    assert result.ids == ("tests/test_a.py::test_ok",)
    assert len(result.dropped_unusable) == 2
    assert result.sampling.total_pool == 1


def a_candidate() -> Candidate:
    """一条能组装成题目的最小候选。"""
    return Candidate(
        repo_name="pallets/click",
        pr_number=4242,
        base_commit="a" * 40,
        base_ref_name="main",
        issue_title="分页器在生成器抛异常时会漏出已写入的内容",
        issue_body="调用 echo_via_pager 时，如果传进去的生成器中途抛异常，"
        "已经写进分页器的那一段仍然会显示出来。期望是一个字都不显示。" * 3,
        issue_language=IssueLanguage.ZH,
        f2p_candidates=("tests/test_x.py::test_new",),
        test_patch=(
            "diff --git a/tests/test_x.py b/tests/test_x.py\n"
            "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n"
            "@@ -1,1 +1,2 @@\n def test_old():\n+    pass\n"
        ),
        gold_patch=(
            "diff --git a/src/click/_termui_impl.py b/src/click/_termui_impl.py\n"
            "--- a/src/click/_termui_impl.py\n+++ b/src/click/_termui_impl.py\n"
            "@@ -1,1 +1,2 @@\n x = 1\n+y = 2\n"
        ),
    )


def test_assemble_drops_flaky_even_from_a_cached_list() -> None:
    """**兜底那一道**：探测轮缓存下来的 P2P 清单是按老规则算的，
    组装时不再走 `select_p2p()`，所以判据必须也在 `assemble()` 里。

    2026-09-10 实测踩到：只在 `select_p2p()` 里加过滤，重跑 assemble
    显示"更新 22"，而 1123 条 pager 用例一条没少。
    """
    task = assemble(
        a_candidate(),
        environment_from_recipe(parse_recipe(RECIPE)),
        dataset_id="test-dev",
        fail_to_pass=["tests/test_x.py::test_new"],
        pass_to_pass=[
            "tests/test_a.py::test_ok",
            "tests/test_utils.py::test_echo_via_pager[test5-less]",
        ],
        p2p_sampling=P2PSampling(strategy="full", seed=None, total_pool=2),
    )
    assert list(task.pass_to_pass) == ["tests/test_a.py::test_ok"]
    # `full` 的定义是"候选池全收"，池子小了这个数要跟着小
    assert task.p2p_sampling is not None
    assert task.p2p_sampling.total_pool == 1


# ── 探测的全量套件超时（E8-T3）────────────────────────────────


def test_probe_suite_timeout_is_separate_from_the_task_runtime_budget() -> None:
    """探测跑全量要用自己的超时，不能被题目运行期那 480 秒卡住。

    两者管的是两件事：

    - 题目上的 `test_timeout_s`（480）管**运行期**。§7.7 规定套件超过 3 分钟时
      P2P 取子集，正式评测只跑 `F2P ∪ P2P 子集` —— 那才是 MET-02 六小时预算里的项。
    - 探测必须跑**全量**，因为 P2P 候选池就是从那份全量报告推出来的（§7.2(6)）。

    用 480 卡探测，等于把"全量超过 8 分钟的仓库"整个挡在门外，而 §7.7 本来就是
    为这种仓库写的。2026-09-13 实测：`sqlfluff` 全量 8152 条用例跑 663 秒，
    按 480 卡时三条好候选全被判成 `TEST_TOO_SLOW`。
    """
    from dataclasses import fields

    from app.domain.execution_plan import ExecutionPlan
    from cli.promote import PROBE_SUITE_TIMEOUT_S

    # ExecutionPlan 是 slots 的 dataclass，类属性拿到的是 member_descriptor 不是默认值
    runtime_budget = next(f.default for f in fields(ExecutionPlan) if f.name == "test_timeout_s")
    assert runtime_budget < PROBE_SUITE_TIMEOUT_S, (
        "探测的超时必须比题目运行期的宽，否则套件长的仓库一条题都出不来"
    )


def test_probe_parser_exposes_suite_timeout() -> None:
    from cli.promote import PROBE_SUITE_TIMEOUT_S, build_parser

    parser = build_parser()
    assert parser.parse_args(["probe"]).suite_timeout == PROBE_SUITE_TIMEOUT_S
    assert parser.parse_args(["probe", "--suite-timeout", "900"]).suite_timeout == 900
