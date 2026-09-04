"""沙箱：Docker 封装、工作区物化、镜像构建、资源限额、网络策略。

已经落地的是工作区这一半（E2-T1）：

    from app.sandbox import MirrorManager, materialize_workspace

    mirrors = MirrorManager(settings.mirror_root)
    mirror = mirrors.ensure_commit(repo_name, repo_url, base_commit)
    ws = materialize_workspace(mirror_path=mirror, base_commit=base_commit, dest=agent_dir)
    # ws.path 交给 Agent 容器绑定挂载；测试阶段再物化一次到另一个目录（§10.2）

依赖规则：可依赖 storage / infrastructure / domain。
禁止依赖 runner —— 是 runner 用 sandbox，不能反过来。
"""

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
    "DEFAULT_WORKSPACE_IGNORE",
    "CommitNotFoundError",
    "GitError",
    "GitResult",
    "MirrorError",
    "MirrorManager",
    "Workspace",
    "WorkspaceError",
    "materialize_workspace",
    "mirror_dir_name",
    "remove_workspace",
    "run_git",
    "validate_commit",
    "validate_repo_name",
]
