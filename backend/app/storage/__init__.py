"""制品存储：`ArtifactStore` 接口 + 本地文件系统 / MinIO 两种实现。

用法：

    from app.storage import create_artifact_store

    store = create_artifact_store()
    ref = store.put("runs/1/task-runs/2/agent_stdout.log", data, content_type="text/plain")
    # ref 的字段和 artifacts 表的列对得上，直接建行

依赖规则：可依赖 infrastructure / domain。
"""

from app.domain.enums import ArtifactBackend
from app.infrastructure.config import Settings, get_settings
from app.storage.base import (
    ArtifactNotFoundError,
    ArtifactRef,
    ArtifactStore,
    InvalidArtifactKeyError,
    validate_key,
)
from app.storage.local import LocalArtifactStore


def create_artifact_store(settings: Settings | None = None) -> ArtifactStore:
    """按配置建一个制品存储。

    这就是 ADR-005 说的"一个配置项切换后端"：业务代码只认 `ArtifactStore` 接口，
    换后端不用改任何调用点。
    """
    settings = settings or get_settings()
    if settings.artifact_backend is ArtifactBackend.LOCAL:
        return LocalArtifactStore(settings.artifact_local_root)
    # MinIO 是 E10-T2 的活（Week 3）。这里明确报错，不要静默退回本地 ——
    # 那样会让"以为存进对象存储了、其实写在容器本地磁盘上"这种问题拖到演示时才发现。
    raise NotImplementedError(
        f"制品后端 {settings.artifact_backend} 还没实现（MinIO 见 E10-T2），"
        f"当前请用 ARTIFACT_BACKEND=local"
    )


__all__ = [
    "ArtifactNotFoundError",
    "ArtifactRef",
    "ArtifactStore",
    "InvalidArtifactKeyError",
    "LocalArtifactStore",
    "create_artifact_store",
    "validate_key",
]
