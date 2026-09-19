"""Worker 领取新作业前的磁盘水位门禁（E9-T3）。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.domain.enums import ArtifactBackend
from app.infrastructure.logging import get_logger
from app.sandbox.images import DiskHeadroom, disk_headroom, docker_root_dir

if TYPE_CHECKING:
    from app.infrastructure.config import Settings

logger = get_logger(__name__)

DiskReader = Callable[[], Sequence[DiskHeadroom]]


def relevant_disk_headrooms(
    settings: Settings, *, docker_client: Any = None
) -> tuple[DiskHeadroom, ...]:
    """读取工作区、制品和 Docker 存储所在分区的剩余空间。

    三个目录可能在同一分区，重复读不会改变判定；保留路径能让告警直接说明是哪一处
    低于水位。任一路径无法读取时向上抛出，由 ``DiskGate`` 保守暂停领取。
    """
    paths = [Path(settings.workspace_root)]
    if settings.artifact_backend is ArtifactBackend.LOCAL:
        paths.append(Path(settings.artifact_local_root))
    paths.append(docker_root_dir(docker_client))

    unique: dict[str, Path] = {}
    for path in paths:
        unique.setdefault(str(path), path)
    return tuple(disk_headroom(path) for path in unique.values())


class DiskGate:
    """磁盘不足时暂停领取新作业，恢复后自动放行。"""

    def __init__(self, *, min_free_ratio: float, reader: DiskReader) -> None:
        self.min_free_ratio = min_free_ratio
        self._reader = reader
        self._blocked = False

    def allows_new_jobs(self) -> bool:
        """返回当前是否可以领取新作业，并只在状态变化时记录告警。"""
        if self.min_free_ratio <= 0:
            return True
        try:
            readings = tuple(self._reader())
        except Exception as exc:
            if not self._blocked:
                logger.error(
                    "worker_disk_check_failed",
                    error=f"{type(exc).__name__}: {exc}",
                    detail="无法确认磁盘余量，暂停领取新作业",
                )
            self._blocked = True
            return False

        low = tuple(item for item in readings if item.free_ratio < self.min_free_ratio)
        if low:
            if not self._blocked:
                logger.warning(
                    "worker_disk_low",
                    min_free_ratio=self.min_free_ratio,
                    volumes=[
                        {
                            "path": str(item.path),
                            "free_gib": round(item.free_gib, 2),
                            "free_ratio": round(item.free_ratio, 4),
                        }
                        for item in low
                    ],
                    detail="暂停领取新作业，已在运行的作业继续收尾",
                )
            self._blocked = True
            return False

        if self._blocked:
            logger.info(
                "worker_disk_recovered",
                min_free_ratio=self.min_free_ratio,
                volumes=[
                    {
                        "path": str(item.path),
                        "free_gib": round(item.free_gib, 2),
                        "free_ratio": round(item.free_ratio, 4),
                    }
                    for item in readings
                ],
            )
        self._blocked = False
        return True


__all__ = ["DiskGate", "DiskReader", "relevant_disk_headrooms"]
