"""容器执行器的四条负例和加固项（E2-T2 的验收标准）。

这一组真的会起 Docker 容器，所以带 `docker` 标记 —— `make test` 和 CI 都跳过它，
要跑得用 `make test-docker`。

四条负例是 E2-T2 的 AC：

| # | 负例 | 断言的是 |
|:-:|:---|:---|
| ① | 内存炸弹 | `oom_killed` 为真 → `OOM_KILLED` |
| ② | fork 炸弹 | 进程数被 `pids_limit` 卡住 |
| ③ | 死循环 | 到点被杀，且容器不残留 |
| ④ | `--network none` | 连不出去 |

**最要紧的是 `test_oom_and_timeout_look_identical_by_exit_code`**：①③ 的退出码都是 137，
唯一能分开它们的是 `.State.OOMKilled`（协议 C-06、C-07）。这条把两种情况放在一起跑，
钉死"退出码相同、判定不同"。
"""

from __future__ import annotations

import json
import os
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.domain.enums import InfraOutcome
from app.sandbox.container import (
    BENCH_LABEL,
    BENCH_LABEL_VALUE,
    BENCH_RUN_LABEL,
    DETERMINISM_ENV,
    WORKSPACE_TARGET,
    BindMount,
    ContainerResult,
    ContainerSpec,
    ImageNotFoundError,
    NetworkMode,
    ResourceLimits,
    Stage,
    build_env,
    classify_outcome,
    get_docker_client,
    kill_containers_by_run_prefix,
    reap_orphans,
    run_in_container,
)

pytestmark = pytest.mark.docker

#: 测试用的镜像。默认用 `bench-base` 将来要基于的那个（§10.4 Layer 1），
#: 换机器可以用 BENCH_SANDBOX_TEST_IMAGE 指到别的。
TEST_IMAGE = os.environ.get("BENCH_SANDBOX_TEST_IMAGE", "python:3.11-slim")

#: 假密钥，分段拼出来 —— 整串写会被密钥扫描器扫到自己头上。
FAKE_KEY = "sk-" + "test" + "-" + "0" * 20


def script(source: str) -> list[str]:
    """把一段 Python 源码包成 `python -c` 的命令。

    命令用列表不用字符串：字符串形式要经过 shell，路径里一个空格就能改变命令的含义。
    """
    return ["python", "-c", textwrap.dedent(source).strip()]


#: 真的去写内存页。`bytearray(n)` 可能只是要一块懒分配的零页，不一定触发 OOM。
MEMORY_BOMB = script(
    """
    blocks = []
    while True:
        blocks.append(b"x" * (10 * 1024 * 1024))
    """
)

#: fork 到失败为止，把成功的次数打出来。到达 pids 上限时 Python 抛 BlockingIOError。
FORK_BOMB = script(
    """
    import os, sys, time
    n = 0
    while n < 500:
        try:
            pid = os.fork()
        except BlockingIOError:
            print("forked=%d" % n)
            sys.exit(0)
        if pid == 0:
            time.sleep(20)
            os._exit(0)
        n += 1
    print("no-limit")
    sys.exit(1)
    """
)

#: 死循环，并且**故意忽略 SIGTERM** —— 这样才能验证宽限期过后我们真的会升级到 SIGKILL。
#: 只写个 while True 的话，SIGTERM 就把它收掉了，走不到 kill 那一步。
IGNORE_SIGTERM_LOOP = script(
    """
    import signal, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.2)
    """
)

DIAL_OUT = script(
    """
    import socket
    socket.create_connection(("1.1.1.1", 443), timeout=3)
    print("connected")
    """
)


@pytest.fixture(scope="module")
def client() -> Any:
    """连 docker daemon，连不上就跳过整个模块。"""
    from docker.errors import DockerException

    try:
        docker_client = get_docker_client()
        docker_client.ping()
    except (DockerException, OSError) as exc:
        pytest.skip(f"docker daemon 不可用：{exc}")
    return docker_client


@pytest.fixture(scope="module")
def image(client: Any) -> str:
    """确保测试镜像在本地，不在就拉一次。

    执行器自己**不会**拉镜像（评测中途拉镜像会打爆时间预算），所以这一步得测试来做。
    拉不下来就跳过，并把原因说清楚 —— 这台机器上装了两套 docker，
    它们的镜像不共享，很容易"明明拉过却找不到"。
    """
    from docker.errors import DockerException, ImageNotFound

    try:
        client.images.get(TEST_IMAGE)
    except ImageNotFound:
        try:
            client.images.pull(TEST_IMAGE)
        except DockerException as exc:
            pytest.skip(f"拉不到 {TEST_IMAGE}：{exc}")
    return TEST_IMAGE


@pytest.fixture
def spec_factory(image: str) -> Any:
    """造一个基础 `ContainerSpec`，用例只改自己关心的字段。"""

    def make(command: list[str], **overrides: Any) -> ContainerSpec:
        params: dict[str, Any] = {
            "image": image,
            "command": command,
            "timeout_s": 60,
            "stage": Stage.TEST,
            "network": NetworkMode.NONE,
            "limits": ResourceLimits(cpus=1.0, memory_mb=512, pids_limit=64, tmpfs_mb=64),
        }
        params.update(overrides)
        return ContainerSpec(**params)

    return make


@pytest.fixture(autouse=True)
def no_leftovers(client: Any) -> Iterator[None]:
    """每条用例跑完，带 bench 标签的容器必须一个不剩。

    容器残留不会立刻报错，只会慢慢吃掉内存和 pid，等到几十道题之后才以
    "资源不够"的形式爆出来，那时候已经查不到是哪次运行漏的了。
    """
    yield
    leftovers = client.containers.list(
        all=True, filters={"label": f"{BENCH_LABEL}={BENCH_LABEL_VALUE}"}
    )
    names = [c.name for c in leftovers]
    for container in leftovers:  # 别让一条用例的残留污染后面的用例
        container.remove(force=True)
    assert not names, f"有容器没删掉：{names}"


# ── 先确认正常路径是通的 ────────────────────────────────────


def test_hello_world_runs_and_is_removed(spec_factory: Any, client: Any) -> None:
    result = run_in_container(spec_factory(script('print("hello")')))
    assert result.ok
    assert result.stdout.strip() == "hello"
    assert result.exit_code == 0
    assert client.containers.list(all=True, filters={"id": result.container_id}) == []


def test_streams_are_kept_apart(spec_factory: Any) -> None:
    """stdout 和 stderr 分开收。合流之后就分不出哪句是报错了。"""
    result = run_in_container(
        spec_factory(
            script(
                """
                import sys
                print("to-stdout")
                print("to-stderr", file=sys.stderr)
                """
            )
        )
    )
    assert "to-stdout" in result.stdout
    assert "to-stderr" not in result.stdout
    assert "to-stderr" in result.stderr


# ── AC ①：内存炸弹 → OOM_KILLED ─────────────────────────────


def test_memory_bomb_is_reported_as_oom(spec_factory: Any) -> None:
    """AC ①：申请超过 `--memory` 的内存，判据是 `.State.OOMKilled`。"""
    spec = spec_factory(MEMORY_BOMB, limits=ResourceLimits(memory_mb=256, pids_limit=64))
    result = run_in_container(spec)

    assert result.oom_killed
    assert not result.timed_out
    assert classify_outcome(result, stage=Stage.TEST) is InfraOutcome.OOM_KILLED


# ── AC ③ 与坑 5.4：超时，以及"退出码分不出这两者" ─────────────


def test_oom_and_timeout_look_identical_by_exit_code(spec_factory: Any) -> None:
    """两种情况的退出码相同，判定相反 —— 这就是协议 C-07 禁止用退出码的原因。

    单独跑 OOM 或者单独跑超时，都看不出这个问题；放在一起跑才能把它钉死。
    """
    oom = run_in_container(
        spec_factory(MEMORY_BOMB, limits=ResourceLimits(memory_mb=256, pids_limit=64))
    )
    timeout = run_in_container(spec_factory(IGNORE_SIGTERM_LOOP, timeout_s=2, stop_grace_s=1))

    assert oom.exit_code == timeout.exit_code  # 都是 137
    assert oom.oom_killed and not timeout.oom_killed
    assert classify_outcome(oom, stage=Stage.AGENT) is InfraOutcome.OOM_KILLED
    assert classify_outcome(timeout, stage=Stage.AGENT) is InfraOutcome.AGENT_TIMEOUT


def test_infinite_loop_is_killed_on_time(spec_factory: Any, client: Any) -> None:
    """AC ③：死循环按时被杀，容器不残留。

    被测程序**忽略了 SIGTERM**，所以这条同时验证了宽限期过后我们会升级到 SIGKILL。
    时间只卡下界和一个宽松的上界：卡死具体秒数会让这条用例在机器负载高时随机变红。
    """
    timeout_s, grace_s = 2, 1
    result = run_in_container(
        spec_factory(IGNORE_SIGTERM_LOOP, timeout_s=timeout_s, stop_grace_s=grace_s)
    )

    assert result.timed_out
    assert not result.oom_killed
    assert result.duration_s >= timeout_s
    assert result.duration_s < timeout_s + grace_s + 30
    assert client.containers.list(all=True, filters={"id": result.container_id}) == []


# ── AC ②：fork 炸弹 → 被 pids 限制拒绝 ──────────────────────


def test_fork_bomb_hits_the_pids_limit(spec_factory: Any) -> None:
    """AC ②：fork 到失败为止，成功次数必须小于上限。

    断言写成"小于上限"而不是某个具体数字：能 fork 几次还取决于容器里本来有几个进程，
    钉死数字会让这条用例在换镜像之后无缘无故变红。
    """
    pids_limit = 32
    result = run_in_container(
        spec_factory(FORK_BOMB, limits=ResourceLimits(memory_mb=512, pids_limit=pids_limit))
    )

    assert "no-limit" not in result.stdout, "fork 没有被拦住，pids_limit 没生效"
    forked = int(result.stdout.strip().removeprefix("forked="))
    assert 0 < forked < pids_limit
    assert not result.oom_killed  # 是 pid 用完了，不是内存不够


# ── AC ④：--network none 断网 ───────────────────────────────


def test_network_none_blocks_outbound(spec_factory: Any) -> None:
    """AC ④：测试阶段必须连不出去（协议 C-31）。"""
    result = run_in_container(spec_factory(DIAL_OUT, network=NetworkMode.NONE))

    assert result.exit_code != 0
    assert "connected" not in result.stdout
    assert "unreachable" in result.stderr.lower()


# ── 限额真的落到内核了吗 ────────────────────────────────────


def test_limits_reach_the_cgroup(spec_factory: Any) -> None:
    """把限额从容器**内部**读一遍。

    只断言"参数传给了 docker"是不够的：cgroup v2 下这几个文件就是内核实际执行的值，
    读出来对得上，才算限额真的生效了。
    """
    limits = ResourceLimits(cpus=0.5, memory_mb=256, pids_limit=32)
    cgroup_files = ["memory.max", "pids.max", "cpu.max"]
    result = run_in_container(
        spec_factory(["cat", *(f"/sys/fs/cgroup/{name}" for name in cgroup_files)], limits=limits)
    )
    assert result.ok, result.stderr
    memory_max, pids_max, cpu_max = result.stdout.split("\n")[:3]

    assert int(memory_max) == limits.memory_mb * 1024 * 1024
    assert int(pids_max) == limits.pids_limit
    quota, period = (int(x) for x in cpu_max.split())
    assert quota / period == pytest.approx(limits.cpus)


def test_container_is_not_root_and_has_no_capabilities(spec_factory: Any) -> None:
    """非 root + 丢掉全部 capability（§10.3「文件系统策略」一行）。

    `CapEff` 是进程实际持有的权限位图，全 0 表示连改网卡、挂文件系统都做不到。
    """
    result = run_in_container(
        spec_factory(
            script(
                """
                import os, re
                print(os.getuid())
                status = open("/proc/self/status").read()
                print(re.search(r"CapEff:\\s*(\\S+)", status).group(1))
                """
            )
        )
    )
    assert result.ok, result.stderr
    uid, cap_eff = result.stdout.split()

    assert int(uid) != 0
    assert int(cap_eff, 16) == 0


# ── 环境变量白名单 ──────────────────────────────────────────


def test_only_allowlisted_env_reaches_the_container(
    spec_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """容器里只有我们拼出来的那些变量，宿主机进程的环境不会漏进去。

    宿主机这边故意设两个变量：一个随手编的，一个是真会造成泄题的 `GITHUB_TOKEN`
    （有了它被测 AI 就能去翻原来的修复 PR）。两个都不该出现在容器里。
    """
    monkeypatch.setenv("BENCH_LEAK_PROBE", "leaked")
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_KEY)

    result = run_in_container(
        spec_factory(
            script("import json, os; print(json.dumps(dict(os.environ)))"),
            env=build_env({"OPENAI_API_KEY": FAKE_KEY}),
        )
    )
    assert result.ok, result.stderr
    env = json.loads(result.stdout)

    assert env["OPENAI_API_KEY"] == FAKE_KEY
    for name, value in DETERMINISM_ENV.items():
        assert env[name] == value
    assert "BENCH_LEAK_PROBE" not in env
    assert "GITHUB_TOKEN" not in env


# ── 工作区挂载 ──────────────────────────────────────────────


def test_workspace_mount_is_readable_and_writable(spec_factory: Any, tmp_path: Path) -> None:
    """容器要能读到工作区里的文件，也要能写回去 —— 补丁就是这么捕获的（E3-T3）。"""
    (tmp_path / "hello.txt").write_text("from-host\n", encoding="utf-8")
    result = run_in_container(
        spec_factory(
            script(
                """
                print(open("hello.txt").read().strip())
                open("written.txt", "w").write("from-container")
                """
            ),
            mounts=(BindMount.workspace(tmp_path),),
            workdir=WORKSPACE_TARGET,
        )
    )
    assert result.ok, result.stderr
    assert result.stdout.strip() == "from-host"
    assert (tmp_path / "written.txt").read_text(encoding="utf-8") == "from-container"


def test_read_only_mount_rejects_writes(spec_factory: Any, tmp_path: Path) -> None:
    """只读挂载真的写不进去。判定阶段的官方测试文件要靠它防篡改。"""
    result = run_in_container(
        spec_factory(
            script('open("written.txt", "w").write("nope")'),
            mounts=(BindMount.workspace(tmp_path, read_only=True),),
            workdir=WORKSPACE_TARGET,
        )
    )
    assert not result.ok
    assert "read-only" in result.stderr.lower()
    assert not (tmp_path / "written.txt").exists()


# ── 镜像与清理 ──────────────────────────────────────────────


def test_missing_image_fails_fast(spec_factory: Any) -> None:
    """镜像不在本地就直接报错，不去拉。

    评测中途拉镜像会打爆时间预算，也让结果不可复现 —— 预建镜像是 ADR-008 的要求。
    """
    with pytest.raises(ImageNotFoundError) as exc:
        run_in_container(spec_factory(["true"], image="bench-no-such-image:v0"))
    assert "bench-no-such-image:v0" in str(exc.value)


def test_container_is_removed_even_when_the_command_fails(spec_factory: Any, client: Any) -> None:
    """命令失败也要删容器。删容器写在 finally 里，这条盯的就是它。"""
    result = run_in_container(spec_factory(script("import sys; sys.exit(3)")))
    assert result.exit_code == 3
    assert client.containers.list(all=True, filters={"id": result.container_id}) == []


def test_reap_orphans_cleans_up_after_a_crash(spec_factory: Any, image: str, client: Any) -> None:
    """Worker 被 kill -9 之后留下的容器，靠标签认领回来删掉。

    这里手工建一个带 bench 标签的容器来模拟"没跑到 finally"。
    注意 `reap_orphans()` 默认不看年龄、见到就删 —— 本机真的在跑评测时别执行它。
    """
    orphan = client.containers.create(
        image=image,
        command=["sleep", "30"],
        labels={BENCH_LABEL: BENCH_LABEL_VALUE, "bench.stage": "TEST"},
    )
    orphan.start()

    removed = reap_orphans(client=client)

    assert str(orphan.id) in removed
    assert client.containers.list(all=True, filters={"id": str(orphan.id)}) == []


def test_kill_by_run_prefix_stops_only_that_run(spec_factory: Any, image: str, client: Any) -> None:
    """按 `bench.run_id` 的前缀杀容器，别的实验的容器一根汗毛不动（E5-T2）。

    取消一次实验时要打断的是"正卡在 `container.wait()` 上的那十几分钟"。
    只置一个协作式的取消标志不够 —— 那一段里根本没有检查点。

    这里同时验了另一半：**只 kill 不 remove**。删容器是 `run_in_container()`
    的 `finally` 的事，这里抢着删的话，那边紧接着的 `container.reload()`
    会撞上 404，一次干净的取消就变成一条 HARNESS_ERROR。
    """
    victim: dict[str, ContainerResult] = {}
    spared = client.containers.create(
        image=image,
        command=["sleep", "30"],
        labels={BENCH_LABEL: BENCH_LABEL_VALUE, BENCH_RUN_LABEL: "runs/99/tasks/1/attempt-1"},
    )
    spared.start()

    def body() -> None:
        victim["result"] = run_in_container(
            spec_factory(["sleep", "120"], run_id="runs/77/tasks/1/attempt-1", timeout_s=120)
        )

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not client.containers.list(
            filters={"label": f"{BENCH_RUN_LABEL}=runs/77/tasks/1/attempt-1"}
        ):
            time.sleep(0.1)

        killed = kill_containers_by_run_prefix("runs/77/", client=client)
        assert len(killed) == 1, "该杀的那个没杀到"

        thread.join(timeout=30)
        assert not thread.is_alive(), "容器被杀了，run_in_container 应该立刻返回"
        assert victim["result"].exit_code != 0
        assert client.containers.list(all=True, filters={"id": victim["result"].container_id}) == []

        assert client.containers.list(filters={"id": str(spared.id)}), "别的实验的容器被误杀了"
    finally:
        spared.remove(force=True)
        thread.join(timeout=30)


def test_reap_orphans_spares_young_containers(spec_factory: Any, image: str, client: Any) -> None:
    """`min_age_s` 保护刚起来的容器：多个 Worker 同时跑时，别把别人正在用的删了。"""
    fresh = client.containers.create(
        image=image, command=["sleep", "30"], labels={BENCH_LABEL: BENCH_LABEL_VALUE}
    )
    try:
        assert reap_orphans(client=client, min_age_s=3600) == []
        assert client.containers.list(all=True, filters={"id": str(fresh.id)})
    finally:
        fresh.remove(force=True)
