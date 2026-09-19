"""Worker 磁盘水位门禁（E9-T3）。"""

from __future__ import annotations

from pathlib import Path

from app.sandbox.images import DiskHeadroom
from app.worker.disk import DiskGate


def reading(*, free: int, total: int = 100) -> DiskHeadroom:
    return DiskHeadroom(path=Path("/var/lib/docker"), total_bytes=total, free_bytes=free)


def test_disabled_gate_does_not_read_disk() -> None:
    calls = 0

    def reader() -> tuple[DiskHeadroom, ...]:
        nonlocal calls
        calls += 1
        return (reading(free=0),)

    assert DiskGate(min_free_ratio=0, reader=reader).allows_new_jobs() is True
    assert calls == 0


def test_low_disk_blocks_until_space_recovers() -> None:
    samples = iter(((reading(free=10),), (reading(free=20),)))
    gate = DiskGate(min_free_ratio=0.15, reader=lambda: next(samples))

    assert gate.allows_new_jobs() is False
    assert gate.allows_new_jobs() is True


def test_any_low_volume_blocks_new_jobs() -> None:
    gate = DiskGate(
        min_free_ratio=0.15,
        reader=lambda: (
            DiskHeadroom(path=Path("/workspace"), total_bytes=100, free_bytes=50),
            DiskHeadroom(path=Path("/var/lib/docker"), total_bytes=100, free_bytes=14),
        ),
    )

    assert gate.allows_new_jobs() is False


def test_failed_disk_probe_fails_closed() -> None:
    def broken() -> tuple[DiskHeadroom, ...]:
        raise OSError("statfs failed")

    assert DiskGate(min_free_ratio=0.15, reader=broken).allows_new_jobs() is False
