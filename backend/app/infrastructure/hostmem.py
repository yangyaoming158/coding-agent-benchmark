"""宿主机内存水位（E9-T2）。

只读 `/proc/meminfo` 的两行，给三个地方用：Worker 启动时的容量自检、
沙箱名额的内存刹车、以及 manifest 里记的机器事实。

**口径是 `MemAvailable` 不是 `MemFree`。** `MemFree` 把页缓存算成"已用"，
在跑测试的机器上永远是个吓人的数字，而那部分内存随时可以回收。
`scripts/mem_sample.py` 采样用的是同一个口径，两边的数才能对得上。

读不出来一律返回 `None`（非 Linux、被容器屏蔽、文件格式变了）。
调用方必须能在"读不到"的情况下正常工作 —— 内存读数不该成为跑评测的前提。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Linux 的内存信息文件。测试里换成临时文件。
MEMINFO_PATH = Path("/proc/meminfo")


@dataclass(frozen=True, slots=True)
class HostMemory:
    """一次内存读数，单位统一成 MiB。"""

    total_mb: int
    available_mb: int

    @property
    def used_mb(self) -> int:
        """已用 = 总量 - 可回收。这个数就是容量模型里的"基线"。"""
        return max(0, self.total_mb - self.available_mb)

    @property
    def used_pct(self) -> float:
        return self.used_mb * 100.0 / self.total_mb if self.total_mb else 0.0


def read_host_memory(path: Path = MEMINFO_PATH) -> HostMemory | None:
    """读一次内存水位。读不到返回 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    total = available = None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            total = _kb(line)
        elif line.startswith("MemAvailable:"):
            available = _kb(line)
        if total is not None and available is not None:
            break
    if total is None or available is None or total <= 0:
        return None
    return HostMemory(total_mb=total // 1024, available_mb=max(0, available) // 1024)


def _kb(line: str) -> int | None:
    """`MemTotal:       12245676 kB` → 12245676。取不出数就返回 None。"""
    parts = line.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    return int(parts[1])


__all__ = ["MEMINFO_PATH", "HostMemory", "read_host_memory"]
