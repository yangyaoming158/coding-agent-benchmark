"""双层并发闸门（E5-T2）。

验三件事：

1. 两把信号量各自卡住并发数，互不干扰；
2. 正在等名额的时候取消，能立刻醒过来，不用等到拿到名额；
3. `execute_task_run()` 拿到一个已取消的闸门时，一个容器都不起就返回 `CANCELLED`。

不需要数据库，也不需要 Docker —— 这一层测的是调度语义。
真跑那条链在 `tests/sandbox/test_task_run.py`。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.benchmark.schema import TaskDefinition
from app.domain.enums import InfraOutcome, LifecycleStatus
from app.evaluation.gate import NULL_GATE, TaskCancelledError
from app.evaluation.task_run import TaskRunInputs, deadline_ms, execute_task_run
from app.runner.adapters.noop import NoopRunner
from app.worker.concurrency import ConcurrencyLimits

#: 拿一道真的 Golden 题当输入。自己拼 `AgentTaskInput` 要造四个嵌套对象，
#: 而这个测试关心的根本不是任务长什么样。
GOLDEN_DIR = Path(__file__).resolve().parents[2].parent / "datasets" / "golden"


def a_task() -> TaskDefinition:
    path = sorted(GOLDEN_DIR.glob("bench-golden__*.json"))[0]
    return TaskDefinition.model_validate_json(path.read_text(encoding="utf-8"))


class _Peak:
    """记录同时进入临界区的最大人数。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.now = 0
        self.peak = 0

    def enter(self) -> None:
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)

    def leave(self) -> None:
        with self.lock:
            self.now -= 1


def _hammer(limits: ConcurrencyLimits, phase: str, *, threads: int, hold_s: float) -> int:
    """`threads` 个线程同时去抢同一层的名额，返回观察到的并发峰值。"""
    peak = _Peak()
    start = threading.Barrier(threads)

    def body() -> None:
        gate = limits.gate_for()
        start.wait()
        slot = gate.sandbox() if phase == "sandbox" else gate.agent()
        with slot:
            peak.enter()
            time.sleep(hold_s)
            peak.leave()

    workers = [threading.Thread(target=body) for _ in range(threads)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(timeout=30)
    return peak.peak


def test_sandbox_slots_cap_concurrency() -> None:
    """沙箱名额设 2，6 个线程一起抢，同时最多只能有 2 个在里面。

    这是"8 并发下内存不炸"的根本保证：撑爆内存的是测试容器，
    卡住它们的就是这把信号量。
    """
    limits = ConcurrencyLimits(agent=6, sandbox=2, poll_s=0.02)
    assert _hammer(limits, "sandbox", threads=6, hold_s=0.05) == 2


def test_agent_slots_are_counted_separately() -> None:
    """两层是**两把**信号量：沙箱只剩 1 个名额，不影响 3 个 AI 同时跑。

    合成一把的话，"等大模型返回"会被"跑测试"的名额卡住，吞吐白白掉一截 ——
    这正是 ADR-012 要分两层的原因。
    """
    limits = ConcurrencyLimits(agent=3, sandbox=1, poll_s=0.02)
    assert _hammer(limits, "agent", threads=5, hold_s=0.05) == 3


def test_slots_are_released_even_when_the_body_blows_up() -> None:
    """临界区里抛异常，名额也要还回去。不还的话 Worker 会越跑槽位越少。"""
    limits = ConcurrencyLimits(agent=1, sandbox=1)
    gate = limits.gate_for()
    with pytest.raises(RuntimeError), gate.sandbox():
        raise RuntimeError("boom")
    assert limits.in_use()["sandbox"] == 0
    with gate.sandbox():
        pass  # 还能再拿到，说明确实还回去了


def test_cancel_wakes_up_a_thread_that_is_waiting_for_a_slot() -> None:
    """正在排队等名额的时候被取消，要立刻醒过来抛 `TaskCancelledError`。

    死等的话，取消一次实验得等到最后一个排队的 task 拿到名额才停得下来，
    验收标准里的 30 秒直接没了。
    """
    limits = ConcurrencyLimits(agent=1, sandbox=1, poll_s=0.02)
    holder = limits.gate_for()
    waiter = limits.gate_for()
    raised: list[BaseException] = []

    with holder.sandbox():  # 唯一的名额被占住，下面那个只能排队

        def body() -> None:
            try:
                with waiter.sandbox():
                    pass
            except BaseException as exc:
                raised.append(exc)

        thread = threading.Thread(target=body)
        thread.start()
        time.sleep(0.1)
        assert thread.is_alive(), "名额被占着，这会儿应该还在等"

        started = time.monotonic()
        waiter.cancel_event.set()
        thread.join(timeout=5)
        elapsed = time.monotonic() - started

    assert not thread.is_alive()
    assert len(raised) == 1
    assert isinstance(raised[0], TaskCancelledError)
    # 线画在 3 秒是给慢机器留的余量。真的没修好的话，这个线程会一直等到
    # 外层 with 退出才拿到名额，那时 `raised` 是空的，上面那条断言先挂
    assert elapsed < 3.0, f"取消之后 {elapsed:.2f} 秒才醒，太慢了"


def test_null_gate_does_nothing() -> None:
    """默认闸门不限流也不取消 —— 串行调用方（单测、CLI）的行为一点没变。"""
    with NULL_GATE.sandbox(), NULL_GATE.agent():
        NULL_GATE.raise_if_cancelled()


def test_a_cancelled_gate_short_circuits_the_whole_task_run(tmp_path: Path) -> None:
    """闸门一开始就是取消状态 → 直接收成 `CANCELLED`，工作区都不物化。

    三字段是协议里的合法组合（`CANCELLED / CANCELLED / NULL`，责任人 HUMAN）：
    既不算被测 AI 没修好，也不算平台故障。

    这条同时证明取消不需要 Docker 也不需要镜像仓库：`mirror_path` 指向一个
    不存在的目录，要是真走到物化那一步，拿到的会是 `WORKSPACE_ERROR`。
    """
    limits = ConcurrencyLimits(agent=1, sandbox=1)
    gate = limits.gate_for()
    gate.cancel_event.set()
    task = a_task()

    outcome = execute_task_run(
        NoopRunner(),
        TaskRunInputs(
            plan=task.execution_plan(),
            agent_input=task.agent_task_input(deadline_unix_ms=deadline_ms(60)),
            mirror_path=tmp_path / "no-such-mirror.git",
            scratch_dir=tmp_path / "ws",
        ),
        gate=gate,
    )

    assert outcome.verdict.infra_outcome is InfraOutcome.CANCELLED
    assert outcome.verdict.lifecycle_status is LifecycleStatus.CANCELLED
    assert outcome.verdict.agent_outcome is None
    assert outcome.verdict.counts_as_infra_failure is False
    assert not (tmp_path / "ws" / "agent").exists(), "取消了就不该物化工作区"
