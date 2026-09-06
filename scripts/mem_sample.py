#!/usr/bin/env python3
"""按秒采样内存占用，用来核对"并发跑起来会不会把机器撑爆"（E5-T2 的验收证据）。

    python3 scripts/mem_sample.py --out var/mem-8x.csv        # Ctrl-C 停，打印峰值
    python3 scripts/mem_sample.py --out var/mem.csv --duration 600

为什么单独写个脚本，而不是让 Worker 自己记：**这是验收要的证据，不是产品功能。**
内存水位是整台机器的属性（还有数据库、编辑器、别的容器在用），Worker 只知道自己
起了几个容器，它报不出"这台机器还剩多少内存"。

口径是 `MemAvailable`，不是 `MemFree`。`MemFree` 把页缓存算成"已用"，
在跑测试的机器上永远是个吓人的数字，而那部分内存随时可以回收。

顺带记一列 bench 容器数：并发曲线和内存曲线摆在一起，才能说清峰值是被谁顶上去的。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MEMINFO = Path("/proc/meminfo")
BENCH_LABEL = "bench.owner=coding-agent-benchmark"


@dataclass(frozen=True, slots=True)
class Sample:
    at: datetime
    total_kb: int
    available_kb: int
    containers: int

    @property
    def used_pct(self) -> float:
        return (self.total_kb - self.available_kb) * 100.0 / self.total_kb

    def csv_row(self) -> str:
        used_mb = (self.total_kb - self.available_kb) // 1024
        return (
            f"{self.at.isoformat()},{used_mb},{self.available_kb // 1024},"
            f"{self.used_pct:.2f},{self.containers}\n"
        )


def read_meminfo() -> tuple[int, int]:
    """返回 `(MemTotal, MemAvailable)`，单位 kB。"""
    total = available = 0
    for line in MEMINFO.read_text(encoding="utf-8").splitlines():
        if line.startswith("MemTotal:"):
            total = int(line.split()[1])
        elif line.startswith("MemAvailable:"):
            available = int(line.split()[1])
        if total and available:
            break
    return total, available


def count_containers(docker: str | None) -> int:
    """现在有几个带 bench 标签的容器在跑。Docker 用不了就记 -1，不让采样中断。"""
    if docker is None:
        return -1
    try:
        result = subprocess.run(
            [docker, "ps", "-q", "--filter", f"label={BENCH_LABEL}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return -1
    return len(result.stdout.split())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按秒采样内存占用")
    parser.add_argument("--out", required=True, help="CSV 写到哪")
    parser.add_argument("--interval", type=float, default=1.0, help="采样间隔（秒）")
    parser.add_argument("--duration", type=float, help="采多久（秒）。不给就一直采到 Ctrl-C")
    args = parser.parse_args(argv)

    docker = shutil.which("docker")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    samples: list[Sample] = []
    deadline = time.monotonic() + args.duration if args.duration else None
    with out.open("w", encoding="utf-8") as handle:
        handle.write("timestamp,used_mb,available_mb,used_pct,bench_containers\n")
        try:
            while deadline is None or time.monotonic() < deadline:
                total, available = read_meminfo()
                sample = Sample(
                    at=datetime.now(tz=UTC),
                    total_kb=total,
                    available_kb=available,
                    containers=count_containers(docker),
                )
                samples.append(sample)
                handle.write(sample.csv_row())
                handle.flush()  # 随采随落盘：Ctrl-C 或者机器卡死也不丢数据
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass

    if not samples:
        print("一个样本都没采到")
        return 1

    peak = max(samples, key=lambda s: s.used_pct)
    total_gb = samples[0].total_kb / 1024 / 1024
    print(f"采了 {len(samples)} 个样本，写到 {out}")
    print(f"内存总量      {total_gb:.1f} GiB")
    print(f"峰值占用      {peak.used_pct:.1f}%（{peak.at:%H:%M:%S}，容器 {peak.containers} 个）")
    print(f"平均占用      {sum(s.used_pct for s in samples) / len(samples):.1f}%")
    print(f"容器数峰值    {max(s.containers for s in samples)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
