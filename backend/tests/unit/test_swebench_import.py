"""SWE-bench Verified 官方题导入的纯逻辑（E1-T7，`03-benchmark-spec.md` §8.6）。

不联网、不起容器、不连库。数据行是手工拼的最小样本，字段名照抄 HF 上的列名。
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import pytest

from app.benchmark.swebench_import import (
    BUCKET_GOLD_PROTECTED,
    BUCKET_ISSUE_LEAKS,
    BUCKET_NO_F2P,
    BUCKET_OK,
    BUCKET_REPO_NOT_PYTEST,
    BUCKET_TEST_PATCH_NON_TEST,
    DATASET_ID,
    EXCLUDED_REPOS,
    IMPORT_ROOTS,
    OFFICIAL_ENV_PYTHON,
    OFFICIAL_TEST_TIMEOUT_S,
    PRE_TEST_COMMAND,
    Funnel,
    SwebenchImportError,
    allocate_quotas,
    bucket_environment_id,
    build_task,
    clean_pass_to_pass,
    clean_test_ids,
    fallback_environment,
    load_instances,
    official_environment,
    official_environment_id,
    official_image,
    parse_instance,
    pytest_command_for,
    render_funnel,
    screen,
    stratified_sample,
)
from app.domain.enums import IssueLanguage, TaskDifficulty

BASE = "0" * 39 + "1"
SETUP = "a" * 39 + "b"

TEST_PATCH = """\
diff --git a/tests/test_basic.py b/tests/test_basic.py
--- a/tests/test_basic.py
+++ b/tests/test_basic.py
@@ -1,2 +1,5 @@
 def test_old():
     assert True
+
+def test_new():
+    assert 1 + 1 == 2
"""

GOLD_PATCH = """\
diff --git a/src/flask/app.py b/src/flask/app.py
--- a/src/flask/app.py
+++ b/src/flask/app.py
@@ -1,2 +1,2 @@
-x = 1
+x = 2
 y = 3
"""


def row(**overrides: Any) -> dict[str, Any]:
    """一行官方数据。默认是一道合规的 flask 题，F2P / P2P 按官方的样子存成 JSON 字符串。"""
    data: dict[str, Any] = {
        "repo": "pallets/flask",
        "instance_id": "pallets__flask-4045",
        "base_commit": BASE,
        "patch": GOLD_PATCH,
        "test_patch": TEST_PATCH,
        "problem_statement": "Raise error when blueprint name contains a dot\n\n"
        + "This is a long enough description of the problem to pass the length check. " * 3,
        "hints_text": "",
        "created_at": "2021-05-13T21:32:41Z",
        "version": "2.0",
        "FAIL_TO_PASS": json.dumps(["tests/test_basic.py::test_new"]),
        "PASS_TO_PASS": json.dumps(["tests/test_basic.py::test_old", "[100%]"]),
        "environment_setup_commit": SETUP,
        "difficulty": "<15 min fix",
    }
    data.update(overrides)
    return data


# ══════════════════════════════════════════════════════════════
# 解析
# ══════════════════════════════════════════════════════════════


def test_parse_row_decodes_json_string_lists() -> None:
    instance = parse_instance(row())
    assert instance.FAIL_TO_PASS == ("tests/test_basic.py::test_new",)
    assert instance.PASS_TO_PASS == ("tests/test_basic.py::test_old", "[100%]")
    assert instance.pr_number == 4045
    assert (instance.owner, instance.name) == ("pallets", "flask")


def test_parse_row_accepts_real_lists_too() -> None:
    instance = parse_instance(row(FAIL_TO_PASS=["a::b"], PASS_TO_PASS=[]))
    assert instance.FAIL_TO_PASS == ("a::b",)
    assert instance.PASS_TO_PASS == ()


def test_parse_row_missing_field_is_an_error() -> None:
    data = row()
    del data["base_commit"]
    with pytest.raises(SwebenchImportError, match="缺字段"):
        parse_instance(data)


def test_load_instances_reads_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "verified.jsonl"
    path.write_text(
        json.dumps(row()) + "\n\n" + json.dumps(row(instance_id="pallets__flask-1")) + "\n"
    )
    ids = [instance.instance_id for instance in load_instances(path)]
    assert ids == ["pallets__flask-4045", "pallets__flask-1"]


# ══════════════════════════════════════════════════════════════
# 官方镜像与环境
# ══════════════════════════════════════════════════════════════


def test_official_image_replaces_double_underscore_and_lowercases() -> None:
    """Docker Hub 不许仓库名里有 `__`，官方换成 `_1776_`，并且全小写。"""
    assert (
        official_image("astropy__astropy-12907")
        == "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest"
    )
    assert (
        official_image("scikit-learn__scikit-learn-10297")
        == "swebench/sweb.eval.x86_64.scikit-learn_1776_scikit-learn-10297:latest"
    )


def test_environment_ids() -> None:
    instance = parse_instance(row())
    assert official_environment_id(instance.instance_id) == "swebench__pallets__flask-4045"
    assert bucket_environment_id(instance) == f"swebench__pallets__flask__env-{SETUP[:12]}"


def test_test_command_points_pythonpath_at_the_package_root() -> None:
    """src / lib 布局的仓库要指到包所在那一层，否则 import 到的是 /testbed 里没打补丁的代码。"""
    assert "PYTHONPATH=/workspace/src " in pytest_command_for("pallets/flask")
    assert "PYTHONPATH=/workspace/lib " in pytest_command_for("matplotlib/matplotlib")
    assert "PYTHONPATH=/workspace " in pytest_command_for("astropy/astropy")
    command = pytest_command_for("astropy/astropy")
    assert command.startswith("env PYTHONPATH=")
    assert f"{OFFICIAL_ENV_PYTHON} -m pytest" in command
    assert "--junitxml=report/junit.xml" in command
    assert "--timeout" not in command  # 官方镜像没有 pytest-timeout


def test_pre_test_command_survives_shlex_and_compiles() -> None:
    """执行器用 `shlex.split` 拆命令；拆完第三个参数必须是一段能编译的 python 脚本。"""
    parts = shlex.split(PRE_TEST_COMMAND)
    assert parts[:2] == [OFFICIAL_ENV_PYTHON, "-c"]
    script = parts[2]
    compile(script, "<pre_test>", "exec")
    assert "--ignored" in script  # 只拷 git 忽略的构建产物
    assert "lexists" in script  # 目标已存在就跳过，绝不覆盖


def test_official_environment_binding() -> None:
    instance = parse_instance(row())
    env = official_environment(instance, python_version="3.11.9")
    assert env.kind == "official"
    assert env.image_tag == official_image(instance.instance_id)
    assert env.pre_test_command == PRE_TEST_COMMAND
    assert env.python_version == "3.11.9"
    assert env.spec_row() == {
        "python_version": "3.11.9",
        "extra_protected_paths": [],
        "image_tag": env.image_tag,
    }


def test_fallback_environment_is_honest_about_what_it_does_not_know() -> None:
    instance = parse_instance(row())
    env = fallback_environment(instance)
    assert env.kind == "fallback"
    assert env.environment_id == bucket_environment_id(instance)
    assert env.python_version == "unknown"
    assert env.pre_test_command is None


# ══════════════════════════════════════════════════════════════
# 离线筛
# ══════════════════════════════════════════════════════════════


def test_screen_ok_and_drops_junk_p2p() -> None:
    result = screen(parse_instance(row()))
    assert result.ok
    assert result.dropped_p2p == ("[100%]",)


def test_clean_pass_to_pass_keeps_only_nodeids() -> None:
    kept, dropped = clean_pass_to_pass(["a/b.py::t[1]", "[100%]", "c::d"])
    assert kept == ("a/b.py::t[1]", "c::d")
    assert dropped == ("[100%]",)


def test_clean_test_ids_drops_ids_truncated_at_whitespace() -> None:
    # 官方数据从 pytest 日志按空白切 id，参数里带空格的只剩半截，pytest 会报 not found
    kept, dropped = clean_test_ids(["a.py::test_stem[png-w/", "a.py::t[x-y]", "a.py::t2"])
    assert kept == ("a.py::t[x-y]", "a.py::t2")
    assert dropped == ("a.py::test_stem[png-w/",)


def test_build_task_drops_truncated_f2p_and_screen_rejects_when_none_left() -> None:
    inst = parse_instance(row(FAIL_TO_PASS='["t.py::a[x-The", "t.py::b"]'))
    task = build_task(inst, fallback_environment(inst))
    assert task.fail_to_pass == ["t.py::b"]
    only_bad = parse_instance(row(FAIL_TO_PASS='["t.py::a[x-The"]'))
    assert screen(only_bad).bucket == BUCKET_NO_F2P


@pytest.mark.parametrize("repo", sorted(EXCLUDED_REPOS))
def test_screen_excludes_repos_the_judge_cannot_read(repo: str) -> None:
    result = screen(parse_instance(row(repo=repo, instance_id="x__y-1")))
    assert result.bucket == BUCKET_REPO_NOT_PYTEST
    assert result.detail == EXCLUDED_REPOS[repo]


def test_excluded_and_supported_repos_do_not_overlap() -> None:
    assert not set(EXCLUDED_REPOS) & set(IMPORT_ROOTS)


def test_screen_rejects_test_patch_touching_non_test_path() -> None:
    patch = TEST_PATCH.replace("tests/test_basic.py", "testing/python/integration.py")
    result = screen(parse_instance(row(test_patch=patch)))
    assert result.bucket == BUCKET_TEST_PATCH_NON_TEST
    assert "testing/python/integration.py" in result.detail


def test_screen_rejects_gold_touching_protected_path() -> None:
    patch = GOLD_PATCH.replace("src/flask/app.py", "setup.cfg")
    result = screen(parse_instance(row(patch=patch)))
    assert result.bucket == BUCKET_GOLD_PROTECTED
    assert "setup.cfg" in result.detail


def test_screen_rejects_issue_with_pr_url() -> None:
    statement = row()["problem_statement"] + "\nsee https://github.com/pallets/flask/pull/4046"
    assert screen(parse_instance(row(problem_statement=statement))).bucket == BUCKET_ISSUE_LEAKS


def test_screen_rejects_issue_with_diff() -> None:
    statement = row()["problem_statement"] + "\ndiff --git a/x b/x\n"
    assert screen(parse_instance(row(problem_statement=statement))).bucket == BUCKET_ISSUE_LEAKS


def test_screen_rejects_empty_f2p() -> None:
    assert screen(parse_instance(row(FAIL_TO_PASS="[]"))).bucket == BUCKET_NO_F2P


# ══════════════════════════════════════════════════════════════
# 抽样
# ══════════════════════════════════════════════════════════════


def test_quotas_are_proportional_with_a_floor_of_one() -> None:
    quotas = allocate_quotas({"a": 40, "b": 10, "c": 1}, 10)
    assert sum(quotas.values()) == 10
    assert quotas["c"] == 1  # 非空层至少 1 道
    assert quotas["a"] > quotas["b"] > 0


def test_quotas_cap_at_stratum_size_and_pool_size() -> None:
    assert allocate_quotas({"a": 2, "b": 1}, 10) == {"a": 2, "b": 1}
    assert allocate_quotas({}, 5) == {}
    assert allocate_quotas({"a": 3}, 0) == {"a": 0}


def _pool(n_per_repo: dict[str, int]) -> list[Any]:
    instances = []
    for repo, count in n_per_repo.items():
        owner, name = repo.split("/")
        for index in range(count):
            instances.append(parse_instance(row(repo=repo, instance_id=f"{owner}__{name}-{index}")))
    return instances


def test_sample_is_deterministic_and_stratified() -> None:
    pool = _pool({"pallets/flask": 30, "pydata/xarray": 20, "mwaskom/seaborn": 2})
    first = stratified_sample(pool, size=10, seed=7)
    second = stratified_sample(pool, size=10, seed=7)
    assert first == second
    assert len(first.chosen) == 10 and len(set(first.chosen)) == 10
    assert sum(first.quotas.values()) == 10
    assert first.quotas["mwaskom/seaborn"] == 1
    # 每层抽中的 + 备选 = 该层全部
    for repo, count in {"pallets/flask": 30, "pydata/xarray": 20, "mwaskom/seaborn": 2}.items():
        chosen_here = [i for i in first.chosen if i.startswith(repo.split("/")[0] + "__")]
        assert len(chosen_here) + len(first.replacements[repo]) == count


def test_sample_changes_with_seed_but_keeps_prefix_when_size_grows() -> None:
    pool = _pool({"pallets/flask": 30, "pydata/xarray": 20})
    base = stratified_sample(pool, size=10, seed=1)
    other_seed = stratified_sample(pool, size=10, seed=2)
    assert set(base.chosen) != set(other_seed.chosen)
    bigger = stratified_sample(pool, size=14, seed=1)
    # 总数调大只会多出几道，原来的 10 道原封不动（层内顺序是打乱后固定的）
    assert set(base.chosen) <= set(bigger.chosen)


def test_sample_records_pool_digest() -> None:
    pool = _pool({"pallets/flask": 3})
    sample = stratified_sample(pool, size=2, seed=1)
    payload = sample.to_json()
    assert payload["pool_size"] == 3 and len(payload["pool_digest"]) == 64
    assert payload["seed"] == 1 and payload["size"] == 2


# ══════════════════════════════════════════════════════════════
# 组装
# ══════════════════════════════════════════════════════════════


def test_build_task_maps_every_official_field() -> None:
    """§8.6 那张映射表逐项核对（AC 1）。"""
    instance = parse_instance(row())
    env = official_environment(instance, python_version="3.11.9")
    task = build_task(instance, env)

    assert task.task_id == "pallets__flask-4045"
    assert task.dataset_id == DATASET_ID
    assert task.repo_name == "pallets/flask"
    assert task.repo_url == "https://github.com/pallets/flask.git"
    assert task.base_commit == BASE
    assert task.issue_body == instance.problem_statement
    assert task.issue_title == "Raise error when blueprint name contains a dot"
    assert task.issue_language is IssueLanguage.EN
    assert task.hints_text is None
    assert task.gold_patch == GOLD_PATCH
    assert task.test_patch == TEST_PATCH
    assert task.fail_to_pass == ["tests/test_basic.py::test_new"]
    assert task.pass_to_pass == ["tests/test_basic.py::test_old"]  # `[100%]` 剔掉了
    assert task.p2p_sampling is None
    assert task.environment_id == "swebench__pallets__flask-4045"
    assert task.test_command == pytest_command_for("pallets/flask")
    assert task.pre_test_command == PRE_TEST_COMMAND
    assert task.test_timeout_s == OFFICIAL_TEST_TIMEOUT_S
    assert task.source_pr_url == "https://github.com/pallets/flask/pull/4045"
    assert task.created_at_upstream is not None and task.created_at_upstream.year == 2021
    assert task.difficulty is TaskDifficulty.EASY  # 1 文件 2 行，§7.8
    assert task.content_hash and task.content_hash.startswith("sha256:")


def test_build_task_tags_carry_bucket_and_official_difficulty() -> None:
    instance = parse_instance(row())
    task = build_task(instance, official_environment(instance, python_version="3.11.9"))
    assert {
        "swebench-verified",
        "calibration",
        DATASET_ID,
        "official-image",
        "swebench-version-2.0",
        f"swebench-env-{SETUP[:12]}",
        "swebench-difficulty-15-min-fix",
    } <= set(task.tags)
    fallback = build_task(instance, fallback_environment(instance))
    assert "fallback-env" in fallback.tags and "official-image" not in fallback.tags


def test_build_task_title_is_truncated_for_long_first_line() -> None:
    statement = "x" * 400 + "\n" + "body " * 60
    task = build_task(
        parse_instance(row(problem_statement=statement)),
        fallback_environment(parse_instance(row())),
    )
    assert len(task.issue_title) == 200 and task.issue_title.endswith("…")


# ══════════════════════════════════════════════════════════════
# 漏斗
# ══════════════════════════════════════════════════════════════


def test_render_funnel_lists_every_layer() -> None:
    funnel = Funnel(official_total=500)
    funnel.offline.update({BUCKET_OK: 175, BUCKET_REPO_NOT_PYTEST: 314, BUCKET_ISSUE_LEAKS: 8})
    funnel.sampled = 50
    funnel.image_pulled = 48
    funnel.image_unavailable = ["a__b-1", "c__d-2"]
    funnel.mirror_ready = 50
    funnel.imported = 48
    funnel.validation.update({"VALID": 45, "INVALID(GOLD_NOT_FIXING)": 3})
    funnel.per_repo = {"pallets/flask": {"pool": 1, "sampled": 1, "valid": 1}}
    text = render_funnel(funnel)
    assert "| 官方题数 | 500 |" in text
    assert "REPO_NOT_PYTEST | 314" in text
    assert "| 离线筛通过（抽样池） | 175 |" in text
    assert "拉不到：a__b-1、c__d-2" in text
    assert "INVALID(GOLD_NOT_FIXING) | 3" in text
    assert "| **VALID** | **45** |" in text
    assert "| pallets/flask | 1 | 1 | 1 |" in text
    assert funnel.pool == 175 and funnel.valid == 45
