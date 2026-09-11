"""运行 Manifest 的纯函数（E5-T4）。

这一组不碰数据库、不连 docker。三件事在这里定死：

1. manifest 里**记了什么、没记什么** —— 尤其是"白名单只记名字不记值"，
   记错一次就是把各家的 API Key 写进数据库和入库的指纹文件。
2. 两份 manifest 怎么比 —— 任务卡那句"两次运行的 manifest diff 只在时间戳上不同"
   的机器化定义就是 `diff_manifests()` 的 `pinned` 为空。
3. 镜像按 digest 引用怎么拼（协议 C-36）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.manifest import DATASET_SNAPSHOT_DIGEST_KEY, MANIFEST_VERSION, VOLATILE_KEYS
from app.domain.protocol import PROTOCOL_VERSION
from app.evaluation.manifest import (
    MISSING,
    AgentRef,
    DatasetRef,
    ImageRef,
    RunProvenance,
    block,
    build_manifest,
    determinism_facts,
    diff_manifests,
    pinned_image_ref,
)
from app.infrastructure.models.benchmark import EnvironmentSpec
from app.infrastructure.models.evaluation import EvaluationRun
from app.sandbox.container import AGENT_ENV_ALLOWLIST, DETERMINISM_ENV, digest_reference
from app.worker.handlers.eval_task import _image_for


def make_provenance(**overrides: object) -> RunProvenance:
    """一份完整的凭证。测试只改自己关心的那一项。"""
    base: dict[str, object] = {
        "harness_git_sha": "c0ffee" * 6 + "abcd",
        "dirty": False,
        "dataset": DatasetRef(
            slug="benchmark-dev",
            version="v1",
            benchmark_set_id=7,
            snapshot_task_count=22,
            selected_task_count=22,
            snapshot_digest="sha256:" + "3" * 64,
        ),
        "agent": AgentRef(
            agent_config_id=3,
            name="oracle",
            label="oracle@none",
            agent_version="1.0",
            model_name="none",
            config_hash="0" * 64,
            adapter_class="app.runner.adapters.oracle.OracleRunner",
            params={"temperature": 0},
        ),
        "images": (
            ImageRef(
                environment_id="pallets__click__py311",
                tag="bench-env:pallets__click__py311",
                digest="sha256:" + "f" * 64,
            ),
        ),
        "limits": {"agent_concurrency": 10, "sandbox_concurrency": 5, "job_max_attempts": 3},
        "created_at": datetime(2026, 9, 11, 10, 0, tzinfo=UTC),
        "host": {"docker_version": "29.7.2", "cpu_count": 16},
    }
    base.update(overrides)
    return RunProvenance(**base)  # type: ignore[arg-type]


# ── 记了什么 ────────────────────────────────────────────────


def test_manifest_carries_the_seven_groups_of_facts() -> None:
    """任务卡 Goal 点名的那几样，一样都不能少。"""
    manifest = build_manifest(make_provenance())

    assert manifest["manifest_version"] == MANIFEST_VERSION
    assert manifest["protocol_version"] == PROTOCOL_VERSION
    assert manifest["harness_git_sha"] == "c0ffee" * 6 + "abcd"
    assert manifest["dirty"] is False
    # 数据集哈希在**顶层**，键名沿用 E1-T6 —— `gate_verdict()` 靠这条 JSONB 路径
    # 找门禁实验，埋深一层就得改那条查询，而已发布版本的门禁记录改不了
    assert manifest[DATASET_SNAPSHOT_DIGEST_KEY] == "sha256:" + "3" * 64
    assert manifest["dataset"]["slug"] == "benchmark-dev"
    assert manifest["agent"]["config_hash"] == "0" * 64
    assert manifest["images"]["pallets__click__py311"]["digest"] == "sha256:" + "f" * 64
    assert manifest["limits"]["agent_concurrency"] == 10
    assert manifest["determinism"]["env"]["PYTHONHASHSEED"] == "0"


def test_optional_keys_are_absent_when_unset() -> None:
    """`gate_for` / `replay_of` 没有就不写键。

    写一个 null 进去，diff 里就会多出一行永远存在的噪音。
    """
    manifest = build_manifest(make_provenance())
    assert "gate_for" not in manifest
    assert "replay_of" not in manifest

    gated = build_manifest(make_provenance(gate_for="benchmark-dev@v1", replay_of=12))
    assert gated["gate_for"] == "benchmark-dev@v1"
    assert gated["replay_of"] == 12


def test_env_allowlist_records_names_never_values() -> None:
    """白名单里有各家的 API Key 名字。**只能记名字。**

    记值的话，密钥会同时进数据库和 `datasets/manifests/`（那个目录是入库的）。
    """
    facts = determinism_facts()

    assert facts["agent_env_allowlist"] == sorted(AGENT_ENV_ALLOWLIST)
    assert "ANTHROPIC_API_KEY" in facts["agent_env_allowlist"]
    # 记的是名字组成的列表，不是 名字→值 的字典
    assert isinstance(facts["agent_env_allowlist"], list)
    # 固定的确定性环境变量是另一回事：它们的"值"本身就是规格（TZ=UTC 之类），
    # 而且名单里没有任何密钥
    assert facts["env"] == dict(DETERMINISM_ENV)
    assert not any("KEY" in name or "TOKEN" in name for name in facts["env"])


def test_seed_is_pythonhashseed_not_an_invented_field() -> None:
    """任务卡 Goal 里的"种子"落在 `PYTHONHASHSEED=0` 上。

    平台运行时没有第二个随机源，所以**不另造一个恒为某值的 `seed` 字段** ——
    留一个没有意义的字段比不留更糟（同 §7.11 第八节对 `dirty` 的那条推理）。
    """
    manifest = build_manifest(make_provenance())
    assert "seed" not in manifest
    assert manifest["determinism"]["env"]["PYTHONHASHSEED"] == "0"


def test_subset_task_ids_are_listed_only_when_it_is_a_subset() -> None:
    """投整份快照时不抄题号（摘要已经确定了那一批），投子集时非记不可。"""
    whole = build_manifest(make_provenance())
    assert whole["dataset"]["selected_task_ids"] is None

    partial = build_manifest(
        make_provenance(
            dataset=DatasetRef(
                slug="benchmark-dev",
                version="v1",
                benchmark_set_id=7,
                snapshot_task_count=22,
                selected_task_count=2,
                snapshot_digest="sha256:" + "3" * 64,
                selected_task_ids=("pallets__click-2271", "pallets__click-2365"),
            )
        )
    )
    assert partial["dataset"]["selected_task_ids"] == [
        "pallets__click-2271",
        "pallets__click-2365",
    ]


# ── 怎么比 ──────────────────────────────────────────────────


def test_same_provenance_yields_an_equivalent_manifest() -> None:
    """任务卡的 AC：两次运行的 manifest 差异只在允许不同的键上。"""
    left = build_manifest(make_provenance())
    right = build_manifest(make_provenance())

    result = diff_manifests(left, right)
    assert result.pinned == ()
    assert result.volatile == ()
    assert result.equivalent


def test_only_timestamps_and_host_may_differ() -> None:
    """换一台机器、换一个时刻重放，仍然算等价 —— NFR-02 要的就是异机异时复现。"""
    left = build_manifest(make_provenance())
    right = build_manifest(
        make_provenance(
            created_at=datetime(2026, 9, 12, 8, 30, tzinfo=UTC),
            host={"docker_version": "28.0.0", "cpu_count": 8},
            replay_of=42,
        )
    )

    result = diff_manifests(left, right)
    assert result.equivalent
    assert {item.path for item in result.volatile} == {
        "created_at",
        "host.docker_version",
        "host.cpu_count",
        "replay_of",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("harness_git_sha", "d" * 40),
        ("dirty", True),
        ("protocol_version", "v9.9"),
    ],
)
def test_changed_pinned_fields_break_equivalence(field: str, value: object) -> None:
    left = build_manifest(make_provenance())
    right = build_manifest(make_provenance(**{field: value}))

    result = diff_manifests(left, right)
    assert not result.equivalent
    assert [item.path for item in result.pinned] == [field]


def test_a_changed_image_digest_breaks_equivalence() -> None:
    """镜像换了内容 = 环境换了，两次跑的不是同一件事（协议 C-36）。"""
    left = build_manifest(make_provenance())
    right = build_manifest(
        make_provenance(
            images=(
                ImageRef(
                    environment_id="pallets__click__py311",
                    tag="bench-env:pallets__click__py311",
                    digest="sha256:" + "e" * 64,
                ),
            )
        )
    )

    result = diff_manifests(left, right)
    assert [item.path for item in result.pinned] == ["images.pallets__click__py311.digest"]


def test_a_missing_key_is_reported_not_ignored() -> None:
    """一边有、一边没有，要报成差异。

    当成"相等"的话，往 manifest 里加字段会让所有老运行凭空变得"等价"。
    """
    result = diff_manifests({"a": 1}, {"a": 1, "b": 2})
    assert [(item.path, item.left, item.right) for item in result.pinned] == [("b", MISSING, 2)]


def test_lists_compare_whole_not_by_index() -> None:
    """名单少一项该报一条"这份名单变了"，不是十几行下标错位。"""
    result = diff_manifests(
        {"determinism": {"agent_env_allowlist": ["A", "B", "C"]}},
        {"determinism": {"agent_env_allowlist": ["A", "C"]}},
    )
    assert [item.path for item in result.pinned] == ["determinism.agent_env_allowlist"]


def test_volatile_keys_are_the_machine_definition_of_the_ac() -> None:
    """任务卡那句"只在时间戳上不同"，代码里就是这个集合。"""
    assert set(VOLATILE_KEYS) == {"created_at", "host", "replay_of"}


# ── 镜像按 digest 引用（协议 C-36）──────────────────────────


def test_digest_reference_needs_both_tag_and_digest() -> None:
    """docker 的 digest 引用必须带仓库名，而仓库名只能从 tag 里拆。"""
    assert (
        digest_reference("bench-env:pallets__click__py311", "sha256:abc") == "bench-env@sha256:abc"
    )
    assert digest_reference(None, "sha256:abc") is None
    assert digest_reference("bench-env:x", None) is None


def test_pinned_image_ref_reads_the_manifest() -> None:
    manifest = build_manifest(make_provenance())
    assert pinned_image_ref(manifest, "pallets__click__py311") == "bench-env@sha256:" + "f" * 64
    assert pinned_image_ref(manifest, "没这个环境") is None
    assert pinned_image_ref({}, "pallets__click__py311") is None


def test_block_tolerates_old_manifests() -> None:
    """E5-T4 之前的运行 manifest 是空的，读它不该炸。"""
    assert block({}, "images") == {}
    assert block({"images": "不是字典"}, "images") == {}
    assert block({"images": {"a": 1}}, "images") == {"a": 1}


# ── Worker 起容器用哪个镜像 ────────────────────────────────


def _env_spec() -> EnvironmentSpec:
    return EnvironmentSpec(
        environment_id="pallets__click__py311",
        repository_id=1,
        python_version="3.11",
        install_command="pip install -e .",
        test_command="pytest",
        test_report_path="report/junit.xml",
        image_tag="bench-env:pallets__click__py311",
        # 库里这一列是**最新**的那个 digest，和实验当初钉的可以不一样
        image_digest="sha256:" + "9" * 64,
    )


def test_worker_prefers_the_digest_pinned_in_the_manifest() -> None:
    """镜像重建过之后，`environment_specs.image_digest` 已经是新的那个。

    按库里现值跑的话，manifest 说跑的是 A、实际跑的是 B，而且不报错。
    """
    run = EvaluationRun(name="x", benchmark_set_id=1, agent_config_id=1)
    run.manifest = build_manifest(make_provenance())

    assert _image_for(run, _env_spec()) == "bench-env@sha256:" + "f" * 64


def test_worker_falls_back_to_tag_for_old_runs() -> None:
    """E5-T4 之前建的实验 manifest 是空的，行为和那时一样：按 tag 起。"""
    run = EvaluationRun(name="x", benchmark_set_id=1, agent_config_id=1)
    run.manifest = {}

    assert _image_for(run, _env_spec()) == "bench-env:pallets__click__py311"
