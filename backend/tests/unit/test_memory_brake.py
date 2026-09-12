"""沙箱名额上的内存刹车（E9-T2）。

信号量管的是"几个在跑"，管不了"跑起来会不会把内存吃穿"：同样 4 个测试容器，
跑 click 的测试占几百 MB，跑一道按上限吃满的题就是 6 GB。刹车补的是这一段。

四条：关着的时候什么都不做、低于阈值会等、等不到也要放行、等的时候能被取消。
内存读数由构造参数注入，所以这一层不需要真的把机器内存吃满。
"""

from __future__ import annotations

import threading
import time

import pytest

from app.evaluation.gate import TaskCancelledError
from app.worker.concurrency import ConcurrencyLimits


def _limits(reader: object, **kwargs: object) -> ConcurrencyLimits:
    params: dict[str, object] = {
        "agent": 2,
        "sandbox": 1,
        "poll_s": 0.02,
        "min_available_mb": 2048,
        "memory_wait_timeout_s": 5.0,
        "memory_reader": reader,
    }
    params.update(kwargs)
    return ConcurrencyLimits(**params)  # type: ignore[arg-type]


def test_the_brake_does_nothing_when_it_is_off() -> None:
    """阈值设 0 = 关掉。连内存都不去读 —— CI 和内存充裕的机器用这个口子。"""
    calls = 0

    def reader() -> int:
        nonlocal calls
        calls += 1
        return 0

    limits = _limits(reader, min_available_mb=0)
    with limits.gate_for().sandbox():
        pass
    assert calls == 0


def test_a_missing_reading_is_not_a_reason_to_block() -> None:
    """读不到内存（非 Linux、被容器屏蔽）就照跑。内存读数不该成为跑评测的前提。"""
    limits = _limits(lambda: None)
    with limits.gate_for().sandbox():
        pass


def test_it_waits_until_there_is_room() -> None:
    """一开始内存不够，几次轮询之后够了 —— 等到了才进临界区。"""
    readings = iter([500, 500, 500, 9000])

    def reader() -> int:
        return next(readings, 9000)

    limits = _limits(reader)
    started = time.monotonic()
    with limits.gate_for().sandbox():
        waited = time.monotonic() - started
    assert waited >= 0.04  # 至少轮询过两次


def test_it_gives_up_waiting_instead_of_deadlocking() -> None:
    """一直不够也要放行。

    内存不一定是我们占的：别的进程吃满了内存时死等会让整个 Worker 停摆，
    而它本来还能把手上这道题跑完。超时放行 + 一条告警，比卡死强。
    """
    limits = _limits(lambda: 10, memory_wait_timeout_s=0.1)
    started = time.monotonic()
    with limits.gate_for().sandbox():
        waited = time.monotonic() - started
    assert 0.1 <= waited < 3.0


def test_cancelling_while_waiting_for_memory_releases_the_slot() -> None:
    """等内存的时候被取消：要抛 `TaskCancelledError`，而且**名额要还回去**。

    不还的话，取消一次实验就会永久少一个沙箱名额，而这个 Worker 还会继续跑 ——
    表现是"越跑越慢"，很难查到是取消造成的。
    """
    limits = _limits(lambda: 10, memory_wait_timeout_s=30.0)
    gate = limits.gate_for()

    def cancel_soon() -> None:
        time.sleep(0.05)
        gate.cancel_event.set()

    threading.Thread(target=cancel_soon, daemon=True).start()
    with pytest.raises(TaskCancelledError), gate.sandbox():
        pass

    assert limits.in_use()["sandbox"] == 0
    # 名额真的还回去了：换一个没刹车的闸门立刻就能拿到
    free = limits.gate_for()
    limits.min_available_mb = 0
    with free.sandbox():
        pass


def test_the_agent_phase_has_no_brake() -> None:
    """刹车只管测试容器。Agent 阶段在等大模型返回，卡它只会白白掉吞吐。"""
    limits = _limits(lambda: 10, memory_wait_timeout_s=30.0)
    started = time.monotonic()
    with limits.gate_for().agent():
        pass
    assert time.monotonic() - started < 1.0
