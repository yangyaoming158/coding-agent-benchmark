"""沙箱压测（E9-T2）：并发定档要的两组实测数据。

    python -m cli.stress hold --parallel 5 --mb 1400 --seconds 20
    python -m cli.stress oom  --containers 200 --parallel 5

## 为什么要它：真题压不动这台机器

`benchmark-dev@v1` 那 22 道题（pallets/click）测试阶段平均 4.4 秒、占一百多 MB
（run #99/#100 落库时刻实测）。拿它当压测负载，量到的是启动开销，不是容量。
`01-requirements.md` §4.6 那段 ⚠️ 警告说的就是这件事：**内存数字受题目大小主导**。

所以内存这一层单独压：容器直接调 `run_in_container` 起，不过队列、不进数据库、
不碰数据集。**不造"吃内存的压测题"** —— 那种题要进 `benchmark_tasks`、过八步验证、
`content_hash` 进快照，还得想清楚它算不算 VALID，代价比这个脚本大得多。

## 三条子命令各回答一个问题

**`sweep`：换一组并发数，吞吐和内存各变成什么样。** 真实负载（Oracle × 一版数据集
× 若干轮），每组配置起一个 Worker 跑到队列空，记 makespan、并发三曲线、内存时序。

**`hold`：真的吃满上限会怎样。** N 个容器同时申请并**写满** `--mb` 内存并持有一会儿，
主线程每 0.5 秒采一次宿主水位。回答"这台机器最多同时扛几个满载容器"。

**`oom`：`.State.OOMKilled` 在这个并发下漏报几次。** 每个容器都申请超过限额、
立刻被 OOM 杀掉，数其中有几个的签名是"退出码 137 + 两个标记都是假"
（`ContainerResult.sigkilled_without_oom_flag`）。这是 issue #85 等的那个数：
0 次 → 可以在报告里注明这条已知限制；>0 次 → 必须在 E10-T4 之前改协议 C-06/C-07。

调查记录见 `05-sandbox.md` §10.10。那次是用 `docker events` 对账测出来的，
这里换成直接数 `run_in_container` 的返回值 —— 走的是生产代码那条路，更有说服力。
"""

from __future__ import annotations

import argparse
import os
import statistics
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa

from app.domain.capacity import DEFAULT_SANDBOX_MEMORY_MB
from app.domain.enums import EvaluationRunStatus
from app.evaluation import concurrency as concurrency_mod
from app.infrastructure.config import REPO_ROOT, reset_settings_cache
from app.infrastructure.db import create_db_engine, create_session_factory, session_scope
from app.infrastructure.hostmem import read_host_memory
from app.infrastructure.models.evaluation import EvaluationRun, EvaluationTaskRun
from app.sandbox.container import (
    ContainerResult,
    ContainerSpec,
    NetworkMode,
    ResourceLimits,
    Stage,
    get_docker_client,
    run_in_container,
)
from cli import queue as queue_cli

#: 压测用的镜像。第一层就够 —— 要的只是一个能跑 python 的干净环境。
DEFAULT_IMAGE = "bench-base:py311"


def _script(source: str) -> list[str]:
    """把一段 Python 源码包成 `python -c`。命令用列表不用字符串，不经过 shell。"""
    return ["python", "-c", textwrap.dedent(source).strip()]


def eat_memory(mb: int, seconds: float) -> list[str]:
    """申请 `mb` MiB 并**真的写满**，持有 `seconds` 秒。

    必须逐页写。`bytearray(n)` 可能只拿到一块懒分配的零页，cgroup 根本不记账 ——
    那样测出来的"内存占用"是假的（`tests/sandbox/test_container.py` 的 MEMORY_BOMB
    踩过同一个坑）。
    """
    return _script(
        f"""
        import time
        chunk = b"x" * (1024 * 1024)
        blocks = []
        for _ in range({mb}):
            blocks.append(bytearray(chunk))
        print("allocated", len(blocks), "MiB", flush=True)
        time.sleep({seconds})
        """
    )


@dataclass(frozen=True, slots=True)
class Sample:
    at: float
    available_mb: int
    used_pct: float


class HostSampler:
    """后台每 `interval` 秒采一次宿主内存水位。口径和 `scripts/mem_sample.py` 一致。"""

    def __init__(self, interval: float = 0.5) -> None:
        self.interval = interval
        self.samples: list[Sample] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> HostSampler:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        started = time.monotonic()
        while not self._stop.is_set():
            memory = read_host_memory()
            if memory is not None:
                self.samples.append(
                    Sample(
                        at=round(time.monotonic() - started, 2),
                        available_mb=memory.available_mb,
                        used_pct=memory.used_pct,
                    )
                )
            self._stop.wait(self.interval)

    def write_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["elapsed_s,available_mb,used_pct\n"]
        lines += [f"{s.at},{s.available_mb},{s.used_pct:.2f}\n" for s in self.samples]
        path.write_text("".join(lines), encoding="utf-8")


def _spec(command: list[str], *, image: str, limit_mb: int, timeout_s: int) -> ContainerSpec:
    return ContainerSpec(
        image=image,
        command=command,
        timeout_s=timeout_s,
        stage=Stage.TEST,
        network=NetworkMode.NONE,
        limits=ResourceLimits(cpus=1.0, memory_mb=limit_mb, pids_limit=512),
        run_id="stress",
    )


def _run_many(
    command: list[str], *, count: int, parallel: int, image: str, limit_mb: int, timeout_s: int
) -> list[ContainerResult]:
    """起 `count` 个容器，最多 `parallel` 个同时在跑。"""
    client = get_docker_client()
    spec = _spec(command, image=image, limit_mb=limit_mb, timeout_s=timeout_s)

    def one(_index: int) -> ContainerResult:
        return run_in_container(spec, client=client)

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        return list(pool.map(one, range(count)))


# ── hold ────────────────────────────────────────────────────


def cmd_hold(args: argparse.Namespace) -> int:
    """N 个容器同时吃满内存，看宿主扛不扛得住。"""
    before = read_host_memory()
    if before is None:
        print("读不到 /proc/meminfo，这台机器上跑不了这条命令")
        return 1
    print(
        f"宿主 {before.total_mb} MB，现在可用 {before.available_mb} MB"
        f"（已用 {before.used_pct:.0f}%）"
    )
    print(
        f"起 {args.parallel} 个容器，每个申请 {args.mb} MB、限额 {args.limit_mb} MB、"
        f"持有 {args.seconds} 秒"
    )

    with HostSampler() as sampler:
        started = time.monotonic()
        results = _run_many(
            eat_memory(args.mb, args.seconds),
            count=args.parallel,
            parallel=args.parallel,
            image=args.image,
            limit_mb=args.limit_mb,
            timeout_s=int(args.seconds) + 60,
        )
        elapsed = time.monotonic() - started

    if args.csv:
        sampler.write_csv(Path(args.csv))
        print(f"内存时序已写到 {args.csv}")

    ok = sum(1 for r in results if r.ok)
    oom = sum(1 for r in results if r.oom_killed)
    quiet = sum(1 for r in results if r.sigkilled_without_oom_flag)
    lowest = min((s.available_mb for s in sampler.samples), default=-1)
    peak_pct = max((s.used_pct for s in sampler.samples), default=0.0)

    print(f"\n{args.parallel} 个容器跑完，用时 {elapsed:.1f} 秒")
    print(f"  正常退出        {ok}")
    print(f"  被 OOM 杀掉     {oom}")
    print(f"  静默 SIGKILL    {quiet}   ← 真 OOM 但 docker 没报（#85）")
    print(f"  宿主最低可用    {lowest} MB")
    print(f"  宿主峰值占用    {peak_pct:.1f}%   ← 验收线 80%")
    return 0 if ok == args.parallel else 2


# ── oom ─────────────────────────────────────────────────────

#: 申请多少倍限额。超一点就够被杀，不用夸张 —— 分配越大，进程死之前写的页越多，
#: 一轮压测就越慢。
OOM_FACTOR = 2


def cmd_oom(args: argparse.Namespace) -> int:
    """故意制造真实 OOM，数 `.State.OOMKilled` 漏报几次（issue #85 的决策门）。"""
    limit_mb = args.limit_mb
    want = limit_mb * OOM_FACTOR
    print(f"限额 {limit_mb} MB 的容器里申请 {want} MB，必被 OOM 杀掉")
    print(f"总共 {args.containers} 个，{args.parallel} 路并发")

    started = time.monotonic()
    results = _run_many(
        eat_memory(want, 5),
        count=args.containers,
        parallel=args.parallel,
        image=args.image,
        limit_mb=limit_mb,
        timeout_s=args.timeout_s,
    )
    elapsed = time.monotonic() - started

    flagged = [r for r in results if r.oom_killed]
    quiet = [r for r in results if r.sigkilled_without_oom_flag]
    other = [r for r in results if not r.oom_killed and not r.sigkilled_without_oom_flag]
    rate = len(quiet) * 100.0 / len(results) if results else 0.0
    durations = [r.duration_s for r in results]

    print(f"\n{len(results)} 个容器跑完，用时 {elapsed:.1f} 秒")
    print(f"  OOMKilled=true   {len(flagged)}")
    print(f"  静默 SIGKILL     {len(quiet)}   ← 漏报，占 {rate:.1f}%")
    print(f"  其他             {len(other)}")
    if durations:
        print(f"  单容器耗时       中位 {statistics.median(durations):.2f}s")
    if other:
        sample = other[0]
        print(f"  其他的第一个：exit={sample.exit_code} timed_out={sample.timed_out}")
    print()
    if quiet:
        print(f"漏报 {len(quiet)} 次：协议 C-06/C-07 必须在 E10-T4 之前改（issue #85）")
    else:
        print("这一轮没有漏报。issue #85 可以按'已知限制'处理，但要注明并发数和样本量")
    return 0


# ── sweep ───────────────────────────────────────────────────


def _terminal(status: EvaluationRunStatus) -> bool:
    return status in {
        EvaluationRunStatus.COMPLETED,
        EvaluationRunStatus.PARTIAL,
        EvaluationRunStatus.FAILED,
        EvaluationRunStatus.CANCELLED,
    }


def _enqueue_rounds(
    *, agent: str, slug: str, rounds: int, tag: str, allow_dirty: bool
) -> list[int]:
    """投 `rounds` 轮实验，返回实验号。

    走的是 `cli.queue enqueue` 本身，不另写一套建实验的代码 —— 压测要量的就是
    生产路径，换一套的话量的是别的东西。实验号从库里按名字捞回来，不去解析 CLI 的输出。
    """
    run_ids: list[int] = []
    for index in range(1, rounds + 1):
        name = f"{tag}-r{index}"
        argv = ["enqueue", "--agent", agent, "--name", name, "--set", slug]
        if allow_dirty:
            argv.append("--allow-dirty")
        code = queue_cli.main(argv)
        if code != 0:
            raise RuntimeError(f"投第 {index} 轮失败（{name}），上面有原因")
        factory = create_session_factory(create_db_engine())
        with session_scope(factory) as session:
            run_id = session.execute(
                sa.select(EvaluationRun.id).where(EvaluationRun.name == name)
            ).scalar_one()
        run_ids.append(int(run_id))
    return run_ids


def _wait_for_drain(run_ids: Sequence[int], *, timeout_s: float) -> bool:
    """等这些实验全部走到终态。超时返回 False。"""
    factory = create_session_factory(create_db_engine())
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with session_scope(factory) as session:
            rows = session.execute(
                sa.select(EvaluationRun.status).where(EvaluationRun.id.in_(list(run_ids)))
            ).scalars()
            if all(_terminal(status) for status in rows):
                return True
        time.sleep(2.0)
    return False


def _makespan_s(run_ids: Sequence[int]) -> float:
    """从第一道题开始到最后一道题结束的墙钟时间（秒）。

    口径和 `07-platform-architecture.md` §18.2 一致：**不是**所有题耗时之和。
    """
    factory = create_session_factory(create_db_engine())
    with session_scope(factory) as session:
        row = session.execute(
            sa.select(
                sa.func.min(EvaluationTaskRun.prepare_started_at),
                sa.func.max(EvaluationTaskRun.completed_at),
            ).where(EvaluationTaskRun.evaluation_run_id.in_(list(run_ids)))
        ).one()
    first, last = row[0], row[1]
    if first is None or last is None:
        return 0.0
    return float((last - first).total_seconds())


def cmd_sweep(args: argparse.Namespace) -> int:
    """一组并发数跑一轮真实负载，记 makespan、并发曲线和内存水位。"""
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = args.tag or f"stress-s{args.sandbox}"

    # 环境变量要在**投实验之前**就设好：`collect_provenance()` 把当时的并发数
    # 记进 manifest，不设的话 manifest 记的是默认值，事后对不上这次跑的是哪组
    os.environ["SANDBOX_CONCURRENCY"] = str(args.sandbox)
    os.environ["AGENT_CONCURRENCY"] = str(args.agent_concurrency)
    os.environ["WORKER_SLOTS"] = str(args.slots)
    reset_settings_cache()

    print(
        f"配置 sandbox={args.sandbox} agent={args.agent_concurrency} slots={args.slots}，"
        f"{args.rounds} 轮 × {args.set} "
    )
    run_ids = _enqueue_rounds(
        agent=args.agent,
        slug=args.set,
        rounds=args.rounds,
        tag=tag,
        allow_dirty=args.allow_dirty,
    )
    print(f"实验号 {run_ids}")

    log_path = out_dir / f"{tag}-worker.log"
    started = time.monotonic()
    with HostSampler() as sampler, log_path.open("w", encoding="utf-8") as log:
        worker = subprocess.Popen(
            [sys.executable, "-m", "app.worker"],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
        )
        try:
            drained = _wait_for_drain(run_ids, timeout_s=args.timeout_s)
        finally:
            worker.terminate()
            try:
                worker.wait(timeout=args.shutdown_s)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=30)
    wall_s = time.monotonic() - started

    if not drained:
        print(f"⚠ 等了 {args.timeout_s} 秒还没跑完，下面的数字只反映跑完的那部分")

    sampler.write_csv(out_dir / f"{tag}-mem.csv")
    factory = create_session_factory(create_db_engine())
    with session_scope(factory) as session:
        points = concurrency_mod.series_for(session, list(run_ids))
    (out_dir / f"{tag}-concurrency.csv").write_text(
        concurrency_mod.to_csv(points), encoding="utf-8"
    )

    peak_pct = max((s.used_pct for s in sampler.samples), default=0.0)
    lowest = min((s.available_mb for s in sampler.samples), default=-1)
    quiet_oom = _count_log_event(log_path, "container_sigkilled_without_oom_flag")

    print(f"\n== sandbox={args.sandbox} ==")
    print(f"  makespan        {_makespan_s(run_ids):.1f} s（墙钟 {wall_s:.1f} s，含起停 Worker）")
    for curve in concurrency_mod.CURVES:
        summary = concurrency_mod.summarize(points, curve)
        print(f"  {curve:<14} 峰值 {summary.peak}  P50 {summary.p50}")
    print(f"  内存峰值        {peak_pct:.1f}%（最低可用 {lowest} MB）")
    print(f"  OOM 漏报告警    {quiet_oom} 条")
    print(f"  产物            {out_dir}/{tag}-*.csv、{log_path.name}")
    return 0


def _count_log_event(path: Path, event: str) -> int:
    """数 Worker 日志里某个事件出现几次。日志是结构化的，事件名是固定字符串。"""
    try:
        return path.read_text(encoding="utf-8", errors="replace").count(event)
    except OSError:
        return -1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cli.stress", description="沙箱压测（E9-T2）")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help=f"压测镜像（默认 {DEFAULT_IMAGE}）")
    parser.add_argument(
        "--limit-mb",
        type=int,
        default=DEFAULT_SANDBOX_MEMORY_MB,
        help=f"容器内存限额 MB（默认 {DEFAULT_SANDBOX_MEMORY_MB}，和题目默认值一致）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_hold = sub.add_parser("hold", help="N 个容器同时吃满内存，看宿主水位")
    p_hold.add_argument("--parallel", type=int, default=5, help="同时起几个")
    p_hold.add_argument("--mb", type=int, default=1400, help="每个容器申请多少 MB")
    p_hold.add_argument("--seconds", type=float, default=20.0, help="持有多久")
    p_hold.add_argument("--csv", help="把宿主内存时序写到这个文件")
    p_hold.set_defaults(func=cmd_hold)

    p_oom = sub.add_parser("oom", help="故意 OOM，数 OOMKilled 漏报几次")
    p_oom.add_argument("--containers", type=int, default=200, help="一共起几个")
    p_oom.add_argument("--parallel", type=int, default=5, help="几路并发")
    p_oom.add_argument("--timeout-s", type=int, default=120, help="单容器超时（秒）")
    p_oom.set_defaults(func=cmd_oom)

    p_sweep = sub.add_parser("sweep", help="一组并发数跑一轮真实负载，记 makespan 和内存")
    p_sweep.add_argument("--sandbox", type=int, required=True, help="SANDBOX_CONCURRENCY")
    p_sweep.add_argument("--agent-concurrency", type=int, default=10, help="AGENT_CONCURRENCY")
    p_sweep.add_argument("--slots", type=int, default=8, help="WORKER_SLOTS")
    p_sweep.add_argument("--rounds", type=int, default=5, help="投几轮实验")
    p_sweep.add_argument("--agent", default="oracle", help="用哪个 Agent（默认 oracle，不花钱）")
    p_sweep.add_argument("--set", default="benchmark-dev", help="数据集 slug")
    p_sweep.add_argument(
        "--out",
        default=str(REPO_ROOT / "var" / "stress"),
        help="产物目录（默认仓库根的 var/stress，不受当前工作目录影响）",
    )
    p_sweep.add_argument("--tag", help="产物文件名前缀，默认 stress-s<并发>")
    p_sweep.add_argument("--timeout-s", type=float, default=1800.0, help="最多等多久跑完")
    p_sweep.add_argument("--shutdown-s", type=float, default=300.0, help="Worker 停机宽限")
    p_sweep.add_argument(
        "--allow-dirty",
        action="store_true",
        help="工作区脏也建实验（协议 C-27 的口子）。压测实验本来就不进排行榜",
    )
    p_sweep.set_defaults(func=cmd_sweep)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
