"""运行 Manifest：把一次实验的可复现性事实钉死（E5-T4，协议 C-27、C-28、C-36、C-52）。

    collect_provenance(...)   → 取事实 + 按 C-27 决定拒不拒绝
    build_manifest(prov)      → 拼成要写进 evaluation_runs.manifest 的那个 JSONB
    diff_manifests(a, b)      → 逐字段比两次运行，分开报"必须相同的"和"允许不同的"

## 为什么拆成"取事实"和"写库"两步

协议 C-27 要求工作区不干净时拒绝启动正式实验。强制点必须放在**建实验那一处**
（`app.evaluation.orchestrator.create_runs`），因为生产代码里只有那一个地方建
`EvaluationRun`，三个 CLI 入口和以后的 `POST /api/runs` 全走它 —— 放各个入口的话，
写第四个入口的人一定会漏。

但 `create_runs()` 自己去调 `git status` 是不行的：集成测试也调 `create_runs()`，
而开发时工作区**永远是脏的**，那样每个集成测试都会红。

所以拆开：git 在 `collect_provenance()` 里调、脏工作区在那里拒绝，`create_runs()`
只收一个必填的 `RunProvenance` 然后写库。两件事同时成立 ——
生产路径漏不掉（参数是必填的，建不出 `manifest = {}` 的运行），测试不被误伤
（直接构造 `RunProvenance`，一次 git 都不调）。

## manifest 里只放"启动时就知道"的事实

跑完才知道的（解决率、makespan、重试次数、平台故障数）一律不进，
`evaluation_runs` 上有专门的列。理由是 C-67 那条纪律的推广：**写进去就不许改**。
一个既装启动条件、又装运行结果的 JSONB，必然要被改第二次。

会变的生命周期字段也不进（比如 `benchmark_sets.status`：门禁在 DRAFT 上跑，
发布之后变成 PUBLISHED，把它记进 manifest 会让重放时凭空多出一条差异，
而数据集内容其实一模一样 —— 认数据集身份靠的是摘要，不是状态）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.domain.cost import TokenPrices
from app.domain.manifest import DATASET_SNAPSHOT_DIGEST_KEY, MANIFEST_VERSION, VOLATILE_KEYS
from app.domain.protocol import PROTOCOL_VERSION
from app.infrastructure.gitmeta import git_state
from app.infrastructure.hostmem import read_host_memory
from app.infrastructure.models.agent import Agent, AgentConfig
from app.infrastructure.models.benchmark import BenchmarkSet, BenchmarkTask, EnvironmentSpec
from app.sandbox.container import (
    AGENT_ENV_ALLOWLIST,
    DETERMINISM_ENV,
    digest_reference,
    get_docker_client,
)


class ProvenanceError(RuntimeError):
    """凑不齐可复现性事实，或者工作区不干净。消息是给人看的，直接打到终端上。"""


# ── 事实 ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DatasetRef:
    """这次跑的是哪一版数据集。"""

    slug: str
    version: str
    benchmark_set_id: int
    #: 冻进快照的总题数（`benchmark_sets.task_count`）。
    snapshot_task_count: int
    #: 这次实际投了几道。
    selected_task_count: int
    #: `sha256:` 开头的快照摘要，由调用方现算（`snapshot_digest(items_of(...))`）。
    #: 不在这里算：算它要 `app.benchmark`，而 import-linter 的契约里
    #: `app.evaluation | app.benchmark` 互不可见，CLI 才是唯一的组合层。
    snapshot_digest: str
    #: 只投了快照的一部分时（`experiment start --task`），把那几道题的 id 列出来。
    #:
    #: 投全部时是 None：摘要已经唯一确定了那一批题，再抄一遍是冗余。
    #: 但投子集时**非记不可** —— 摘要覆盖的是 22 道，实际只跑了 3 道，
    #: 光看摘要重放不出同一批题，"可重建"就是假的。
    selected_task_ids: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class AgentRef:
    """这次测的是哪个参赛者。

    排行榜上的参赛者是 `AgentConfig`（Agent × 模型 × 参数），不是 `Agent`，
    所以这里六个字段一个都不能少 —— 同一个 aider 接两个模型是两个参赛者。
    """

    agent_config_id: int
    name: str
    label: str
    agent_version: str
    model_name: str
    #: 参数的规范化哈希。重放时比的是它，不是逐字段比 `params`。
    config_hash: str
    adapter_class: str
    params: dict[str, Any]
    #: 本次运行采用的 token 单价快照。重放必须继续使用这份价格，不能读数据库现值。
    token_prices: TokenPrices = field(default_factory=TokenPrices)


@dataclass(frozen=True, slots=True)
class ImageRef:
    """一个环境用的镜像。

    tag 和 digest 都记：协议 C-36 要求引用镜像用 digest（tag 会被覆盖，digest 不会），
    而 docker 认的 digest 引用形如 `bench-env@sha256:...` —— 仓库名只能从 tag 里拆。
    两个都记下来，Worker 起容器时光看 manifest 就够，不用再回头查 `environment_specs`
    （那一行随时可能被下一次 `cli.images build` 覆盖）。

    `digest` 可能是 None：镜像还没建过。如实记 None，不编一个假的。
    """

    environment_id: str
    tag: str | None
    digest: str | None


@dataclass(frozen=True, slots=True)
class RunProvenance:
    """建一次实验时钉死的全部事实。**只有启动时就知道的。**"""

    harness_git_sha: str
    #: 工作区有未提交改动（协议 C-28）。为真时结果不得进排行榜。
    dirty: bool
    dataset: DatasetRef
    agent: AgentRef
    images: tuple[ImageRef, ...]
    limits: dict[str, int]
    protocol_version: str = PROTOCOL_VERSION
    #: 这份 manifest 拼出来的时刻。允许两次运行不同（`VOLATILE_KEYS`）。
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    #: 跑在什么机器上。取不到就是 None，允许两次运行不同。
    host: dict[str, Any] | None = None
    #: 只有数据集发布门禁的实验有：`benchmark-dev@v1`（E1-T6 定的键，位置不动）。
    gate_for: str | None = None
    #: 这次是重放哪一次运行（`cli.experiment replay` 写）。
    replay_of: int | None = None


# ── 取事实 ──────────────────────────────────────────────────


def collect_provenance(
    session: Session,
    *,
    benchmark_set_id: int,
    snapshot_digest: str,
    agent_config_id: int,
    task_ids: Sequence[int],
    agent_concurrency: int,
    sandbox_concurrency: int,
    job_max_attempts: int,
    allow_dirty: bool = False,
    gate_for: str | None = None,
    replay_of: int | None = None,
) -> RunProvenance:
    """把建实验要钉死的事实凑齐，顺手执行协议 C-27。

    工作区不干净且没给 `allow_dirty` 时抛 `ProvenanceError` —— 这是 C-27 的唯一
    强制点。给了 `allow_dirty`（C-28 的 `--allow-dirty`）就放行，但 `dirty` 如实记为
    真，`evaluation_runs.dirty` 跟着为真，那次实验不得进排行榜。

    `snapshot_digest` 由调用方传进来（理由见 `DatasetRef.snapshot_digest`）。
    """
    sha, dirty = git_state()
    if dirty and not allow_dirty:
        raise ProvenanceError(
            "工作区有未提交的改动，拒绝建实验（协议 C-27）。\n"
            "  实验记录里的 harness_git_sha 只有在工作区干净时才唯一代表一份代码，"
            "否则“可复现”是假的。\n"
            "  先 git commit / git stash，或者加 --allow-dirty 放行"
            "（结果会标 dirty=true，按 C-28 不得进排行榜）。"
        )

    dataset = session.get(BenchmarkSet, benchmark_set_id)
    if dataset is None:
        raise ProvenanceError(f"找不到数据集版本 benchmark_set_id={benchmark_set_id}")

    config = session.get(AgentConfig, agent_config_id)
    if config is None:
        raise ProvenanceError(f"找不到 Agent 配置 agent_config_id={agent_config_id}")
    agent = session.get(Agent, config.agent_id)
    if agent is None:
        raise ProvenanceError(f"找不到 Agent agent_id={config.agent_id}")

    return RunProvenance(
        harness_git_sha=sha,
        dirty=dirty,
        dataset=DatasetRef(
            slug=dataset.slug,
            version=dataset.version,
            benchmark_set_id=dataset.id,
            snapshot_task_count=dataset.task_count,
            selected_task_count=len(task_ids),
            snapshot_digest=snapshot_digest,
            selected_task_ids=_subset_task_ids(session, task_ids, dataset.task_count),
        ),
        agent=AgentRef(
            agent_config_id=config.id,
            name=agent.name,
            label=config.label,
            agent_version=config.agent_version,
            model_name=config.model_name,
            config_hash=config.config_hash,
            adapter_class=agent.adapter_class,
            params=dict(config.params or {}),
            token_prices=TokenPrices(
                input_per_mtok=config.price_input_per_mtok,
                output_per_mtok=config.price_output_per_mtok,
                cache_read_per_mtok=config.price_cache_read_per_mtok,
            ),
        ),
        images=images_for_tasks(session, task_ids),
        limits={
            "agent_concurrency": agent_concurrency,
            "sandbox_concurrency": sandbox_concurrency,
            "job_max_attempts": job_max_attempts,
        },
        host=host_facts(),
        gate_for=gate_for,
        replay_of=replay_of,
    )


def _subset_task_ids(
    session: Session, task_ids: Sequence[int], snapshot_task_count: int
) -> tuple[str, ...] | None:
    """投的是子集就返回那几道题的 `task_id`（排序过），投全部就返回 None。"""
    if not task_ids or len(task_ids) >= snapshot_task_count:
        return None
    rows = session.execute(
        sa.select(BenchmarkTask.task_id).where(BenchmarkTask.id.in_(list(task_ids)))
    ).scalars()
    return tuple(sorted(rows))


def images_for_tasks(session: Session, task_ids: Sequence[int]) -> tuple[ImageRef, ...]:
    """这批题涉及的环境镜像表，按 `environment_id` 排序。

    排序不是为了好看：manifest 要能逐字比对，而 SQL 不保证返回顺序 ——
    同一批题查两次拿到两个顺序，diff 就会报出一堆假的差异。
    """
    if not task_ids:
        return ()
    rows = session.execute(
        sa.select(
            EnvironmentSpec.environment_id, EnvironmentSpec.image_tag, EnvironmentSpec.image_digest
        )
        .join(BenchmarkTask, BenchmarkTask.environment_spec_id == EnvironmentSpec.id)
        .where(BenchmarkTask.id.in_(list(task_ids)))
        .distinct()
        .order_by(EnvironmentSpec.environment_id)
    ).all()
    return tuple(ImageRef(environment_id=row[0], tag=row[1], digest=row[2]) for row in rows)


def host_facts() -> dict[str, Any] | None:
    """跑在什么机器上。**取不到就返回 None，绝不抛异常。**

    为什么要记：NFR-02 要的是"异机异时复现"。两次跑出来结果不一样时，
    第一个要问的是"是不是换机器了、docker 换版本了"，没记就查不了。

    为什么不能抛：`cli.experiment start` 只是建实验投队列，不该因为 docker 没起来
    就建不了实验 —— 真正要 docker 的是 Worker。取不到就如实记 None。
    """
    facts: dict[str, Any] = {"cpu_count": os.cpu_count(), "memory_mb": _memory_mb()}
    try:
        version = get_docker_client().version()
        facts["docker_version"] = str(version.get("Version") or "") or None
        facts["kernel_version"] = str(version.get("KernelVersion") or "") or None
    except Exception:  # 连不上 docker 不该挡住建实验，取不到就如实记 None
        facts["docker_version"] = None
        facts["kernel_version"] = None
    return facts


def _memory_mb() -> int | None:
    """宿主机总内存（MiB）。读不出来返回 None。

    这台机器是 WSL2，内存额度由 `.wslconfig` 控制，`wsl --shutdown` 之后可能变 ——
    记下来才能解释"上次跑得下、这次 OOM"。

    读 `/proc/meminfo` 的活交给 `app.infrastructure.hostmem`（E9-T2 起内存刹车和
    容量自检也读同一份，解析写两遍迟早会漂）。
    """
    memory = read_host_memory()
    return None if memory is None else memory.total_mb


# ── 拼 manifest ─────────────────────────────────────────────


def build_manifest(prov: RunProvenance) -> dict[str, Any]:
    """拼出要写进 `evaluation_runs.manifest` 的那个 JSONB。

    键的分布有意做成"扁平 + 几个块"而不是深层嵌套：`gate_verdict()` 要用
    `manifest['dataset_snapshot_digest']` 这个 JSONB 路径去找门禁实验，
    埋深一层就得改那条查询，而已经发布的 benchmark-dev@v1 的门禁记录改不了
    （C-67 那条纪律：已发布的记录不许事后修改）。
    """
    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "protocol_version": prov.protocol_version,
        "harness_git_sha": prov.harness_git_sha,
        "dirty": prov.dirty,
        # E1-T6 定的键，位置和名字都不动
        DATASET_SNAPSHOT_DIGEST_KEY: prov.dataset.snapshot_digest,
        "dataset": {
            "slug": prov.dataset.slug,
            "version": prov.dataset.version,
            "benchmark_set_id": prov.dataset.benchmark_set_id,
            "snapshot_task_count": prov.dataset.snapshot_task_count,
            "selected_task_count": prov.dataset.selected_task_count,
            "selected_task_ids": (
                list(prov.dataset.selected_task_ids)
                if prov.dataset.selected_task_ids is not None
                else None
            ),
        },
        "agent": {
            "agent_config_id": prov.agent.agent_config_id,
            "name": prov.agent.name,
            "label": prov.agent.label,
            "agent_version": prov.agent.agent_version,
            "model_name": prov.agent.model_name,
            "config_hash": prov.agent.config_hash,
            "adapter_class": prov.agent.adapter_class,
            "params": prov.agent.params,
            "pricing": prov.agent.token_prices.as_manifest(),
        },
        "images": {
            ref.environment_id: {"tag": ref.tag, "digest": ref.digest} for ref in prov.images
        },
        "determinism": determinism_facts(),
        "limits": dict(prov.limits),
        "created_at": prov.created_at.isoformat(),
        "host": prov.host,
    }
    # 两个可选键：没有就不写。写一个 null 进去会让 diff 多出一行噪音
    if prov.gate_for is not None:
        manifest["gate_for"] = prov.gate_for
    if prov.replay_of is not None:
        manifest["replay_of"] = prov.replay_of
    return manifest


def determinism_facts() -> dict[str, Any]:
    """确定性那一组：固定环境变量、环境变量白名单、测试阶段的网络模式。

    **白名单只记名字，绝不记值** —— 名单里有各家的 API Key，记值等于把密钥
    写进数据库和 git（`datasets/manifests/` 是入库的）。

    任务卡 Goal 里的"种子"落在这里的 `PYTHONHASHSEED=0` 上。平台运行时没有第二个
    随机源（`random` 只在 `app.benchmark.assembly` 的 P2P 抽样里用，那是建题期，
    种子记在题目定义里、由数据集摘要覆盖）。**不另造一个恒为 42 的 `seed` 字段** ——
    留一个没有意义的字段，比不留更糟。
    """
    return {
        "env": dict(DETERMINISM_ENV),
        "agent_env_allowlist": sorted(AGENT_ENV_ALLOWLIST),
        "test_network": "none",
    }


# ── 读 manifest ─────────────────────────────────────────────


def block(manifest: Mapping[str, Any], key: str) -> dict[str, Any]:
    """取 manifest 里的一个块（`dataset` / `agent` / `images` / `limits` …）。

    取不到、或者取到的不是字典时返回空字典。老运行的 manifest 可能缺块
    （E5-T4 之前是空的），调用方不该为此写一堆 isinstance。
    """
    value = manifest.get(key)
    return dict(value) if isinstance(value, Mapping) else {}


def pinned_image_ref(manifest: Mapping[str, Any], environment_id: str) -> str | None:
    """manifest 里给这个环境钉死的镜像引用（`bench-env@sha256:...`）。

    **Worker 起容器时按这个走，不按 `environment_specs` 现在那一行**（协议 C-36）。
    两者的区别只有在镜像被重建之后才看得出来：那一列会被新 digest 覆盖，
    而这次实验当初钉的是旧的那个。按库里现值跑的话，manifest 记的就是一句空话 ——
    它说跑的是 A，实际跑的是 B，而且不报错。

    manifest 里没记（老运行、或者那个环境还没建过镜像）时返回 None，
    由调用方退回按 tag 起。
    """
    entry = block(manifest, "images").get(environment_id)
    if not isinstance(entry, Mapping):
        return None
    tag = entry.get("tag")
    digest = entry.get("digest")
    return digest_reference(
        str(tag) if tag else None,
        str(digest) if digest else None,
    )


def pinned_token_prices(manifest: Mapping[str, Any]) -> TokenPrices | None:
    """读取 manifest 中冻结的 token 单价；旧 manifest 没有该块时返回 None。

    新 manifest 一旦写了 `pricing`，哪怕三项都是 null，也表示“启动时没有配置
    单价”。不能退回数据库现值，否则后来补价格会悄悄改变这次实验的成本口径。
    """
    agent = block(manifest, "agent")
    if "pricing" not in agent:
        return None
    prices = TokenPrices.from_manifest(agent["pricing"])
    if prices is None:
        raise ProvenanceError("manifest.agent.pricing 不是合法的 token 单价块")
    return prices


# ── 比两份 manifest ─────────────────────────────────────────


class _Missing:
    """某一边根本没有这个键。单独一个哨兵值，免得和 `None` 混淆 ——
    "没记这个字段"和"记的值是 null"是两件事。"""

    def __repr__(self) -> str:
        return "（没有这个键）"


MISSING = _Missing()


@dataclass(frozen=True, slots=True)
class FieldDiff:
    #: 点分路径，如 `agent.params.temperature`。
    path: str
    left: Any
    right: Any


@dataclass(frozen=True, slots=True)
class ManifestDiff:
    """两份 manifest 的差异，按"允许不允许不同"分成两摞。"""

    #: 必须逐字相同却不同的字段。非空 = 这两次运行不等价。
    pinned: tuple[FieldDiff, ...]
    #: 允许不同的字段（时间戳、主机、重放来源）。
    volatile: tuple[FieldDiff, ...]

    @property
    def equivalent(self) -> bool:
        """两次运行的输入条件是不是等价的。

        注意这**不是**"两次结果会一样"。协议 C-73 写死了测试执行的可复现性是目标
        不是保证，逐实例一致率是 MET-01 的口径（E10-T5），不在这里回答。
        """
        return not self.pinned


def diff_manifests(left: Mapping[str, Any], right: Mapping[str, Any]) -> ManifestDiff:
    """逐字段比两份 manifest。

    只递归进字典，列表整体当一个值比 —— `agent_env_allowlist` 少一项该报成
    "这一项变了"，而不是报成十几行下标错位。
    """
    flat_left = _flatten(left)
    flat_right = _flatten(right)
    pinned: list[FieldDiff] = []
    volatile: list[FieldDiff] = []
    for path in sorted(set(flat_left) | set(flat_right)):
        a = flat_left.get(path, MISSING)
        b = flat_right.get(path, MISSING)
        if a == b:
            continue
        bucket = volatile if path.split(".", 1)[0] in VOLATILE_KEYS else pinned
        bucket.append(FieldDiff(path=path, left=a, right=b))
    return ManifestDiff(pinned=tuple(pinned), volatile=tuple(volatile))


def _flatten(data: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping) and value:
            flat.update(_flatten(value, prefix=f"{path}."))
        else:
            flat[path] = value
    return flat


__all__ = [
    "MISSING",
    "AgentRef",
    "DatasetRef",
    "FieldDiff",
    "ImageRef",
    "ManifestDiff",
    "ProvenanceError",
    "RunProvenance",
    "block",
    "build_manifest",
    "collect_provenance",
    "determinism_facts",
    "diff_manifests",
    "host_facts",
    "images_for_tasks",
    "pinned_image_ref",
    "pinned_token_prices",
]
