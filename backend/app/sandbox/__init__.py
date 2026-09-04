"""沙箱：Docker 封装、工作区物化、镜像构建、资源限额、网络策略。

已经落地的两半：

**工作区（E2-T1）** —— 从 bare mirror 物化出一份没有历史的代码：

    from app.sandbox import MirrorManager, materialize_workspace

    mirrors = MirrorManager(settings.mirror_root)
    mirror = mirrors.ensure_commit(repo_name, repo_url, base_commit)
    ws = materialize_workspace(mirror_path=mirror, base_commit=base_commit, dest=agent_dir)

**容器（E2-T2）** —— 把一条命令关进限额和超时里跑：

    from app.sandbox import BindMount, ContainerSpec, ResourceLimits, run_in_container

    spec = ContainerSpec(
        image="bench-env:demo",
        command=["python", "-m", "pytest", "-q"],
        timeout_s=task.test_timeout_s,
        stage=Stage.TEST,                      # 决定超时记成 TEST_TIMEOUT 还是 AGENT_TIMEOUT
        limits=ResourceLimits(cpus=task.sandbox_cpu, memory_mb=task.sandbox_memory_mb),
        mounts=(BindMount.workspace(ws.path),),
        workdir=WORKSPACE_TARGET,
    )
    result = run_in_container(spec)
    outcome = classify_outcome(result, stage=Stage.TEST)

依赖规则：可依赖 storage / infrastructure / domain。
禁止依赖 runner —— 是 runner 用 sandbox，不能反过来。
"""

from app.sandbox.container import (
    AGENT_ENV_ALLOWLIST,
    BENCH_LABEL,
    BENCH_LABEL_VALUE,
    DETERMINISM_ENV,
    WORKSPACE_TARGET,
    BindMount,
    ContainerResult,
    ContainerSpec,
    DockerUnavailableError,
    EnvNotAllowedError,
    ImageNotFoundError,
    NetworkMode,
    ResourceLimits,
    SandboxError,
    Stage,
    build_env,
    classify_outcome,
    default_container_user,
    get_docker_client,
    reap_orphans,
    run_in_container,
)
from app.sandbox.git_cli import GitError, GitResult, run_git
from app.sandbox.mirror import (
    CommitNotFoundError,
    MirrorError,
    MirrorManager,
    mirror_dir_name,
    validate_commit,
    validate_repo_name,
)
from app.sandbox.workspace import (
    DEFAULT_WORKSPACE_IGNORE,
    Workspace,
    WorkspaceError,
    materialize_workspace,
    remove_workspace,
)

__all__ = [
    "AGENT_ENV_ALLOWLIST",
    "BENCH_LABEL",
    "BENCH_LABEL_VALUE",
    "DEFAULT_WORKSPACE_IGNORE",
    "DETERMINISM_ENV",
    "WORKSPACE_TARGET",
    "BindMount",
    "CommitNotFoundError",
    "ContainerResult",
    "ContainerSpec",
    "DockerUnavailableError",
    "EnvNotAllowedError",
    "GitError",
    "GitResult",
    "ImageNotFoundError",
    "MirrorError",
    "MirrorManager",
    "NetworkMode",
    "ResourceLimits",
    "SandboxError",
    "Stage",
    "Workspace",
    "WorkspaceError",
    "build_env",
    "classify_outcome",
    "default_container_user",
    "get_docker_client",
    "materialize_workspace",
    "mirror_dir_name",
    "reap_orphans",
    "remove_workspace",
    "run_git",
    "run_in_container",
    "validate_commit",
    "validate_repo_name",
]
