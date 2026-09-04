"""容器执行器里不需要 Docker 的那一半（E2-T2）。

这一组全是纯函数：环境变量白名单、结果分类、日志截断、时间戳解析。毫秒级，
`make test` 每次都跑。真的起容器的四条负例在 `test_container.py` 里，带 docker 标记。

**最要紧的是 `test_oom_wins_over_timeout`**：OOM 和超时的退出码都是 137，
分类函数里两个 if 的顺序写反了，内存超限就会被当成超时 —— 前者该重试，
后者直接判 AI 没修好（协议 C-18），一个顺序错误就能让排行榜偏低。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.domain.enums import InfraOutcome
from app.sandbox.container import (
    AGENT_ENV_ALLOWLIST,
    DETERMINISM_ENV,
    WORKSPACE_TARGET,
    BindMount,
    ContainerResult,
    ContainerSpec,
    EnvNotAllowedError,
    NetworkMode,
    ResourceLimits,
    Stage,
    _create_kwargs,
    _read_capped,
    build_env,
    classify_outcome,
    default_container_user,
    parse_docker_time,
)

#: 假密钥。**分段拼出来**，不写成一整串 —— 整串写会被密钥扫描器扫到，
#: 于是我们自己的测试文件成了"仓库里有泄漏密钥"的告警源。
FAKE_KEY = "sk-" + "ant" + "-test-" + "0" * 24


def make_result(**overrides: object) -> ContainerResult:
    """造一个 `ContainerResult`，只写这条用例关心的字段。"""
    fields: dict[str, object] = {
        "container_id": "c0ffee",
        "image": "python:3.11-slim",
        "exit_code": 0,
        "oom_killed": False,
        "timed_out": False,
        "duration_s": 1.0,
        "stdout": "",
        "stderr": "",
    }
    fields.update(overrides)
    return ContainerResult(**fields)  # type: ignore[arg-type]


# ── 结果分类（协议 C-06、C-07、C-19b）────────────────────────


def test_oom_wins_over_timeout() -> None:
    """同时被标成 OOM 和超时时，判 OOM_KILLED。

    这不是假想的组合：容器超内存被杀之后，我们的墙钟可能也正好到点，两个标记会一起为真。
    协议 C-19b 第 1 步写的就是"先确认 OOMKilled = false，若为 true 就不走超时流程"。
    """
    result = make_result(oom_killed=True, timed_out=True, exit_code=137)
    assert classify_outcome(result, stage=Stage.AGENT) is InfraOutcome.OOM_KILLED
    assert classify_outcome(result, stage=Stage.TEST) is InfraOutcome.OOM_KILLED


def test_exit_code_137_alone_is_not_oom() -> None:
    """光看退出码分不出 OOM 和超时 —— 这正是协议 C-07 禁止用退出码判断的原因。"""
    timeout = make_result(exit_code=137, timed_out=True)
    oom = make_result(exit_code=137, oom_killed=True)
    assert timeout.exit_code == oom.exit_code
    assert classify_outcome(timeout, stage=Stage.AGENT) is InfraOutcome.AGENT_TIMEOUT
    assert classify_outcome(oom, stage=Stage.AGENT) is InfraOutcome.OOM_KILLED


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        (Stage.AGENT, InfraOutcome.AGENT_TIMEOUT),
        (Stage.TEST, InfraOutcome.TEST_TIMEOUT),
    ],
)
def test_timeout_is_attributed_to_the_stage(stage: Stage, expected: InfraOutcome) -> None:
    """同样是超时，Agent 阶段和测试阶段记成不同的值。

    C-18 里两者待遇不同：AGENT_TIMEOUT 判 AI 没修好且不重试，
    TEST_TIMEOUT 要走 C-19b 的对照组流程再决定怪谁。
    """
    assert classify_outcome(make_result(timed_out=True), stage=stage) is expected


def test_nonzero_exit_is_not_a_platform_failure() -> None:
    """非零退出码不翻译成故障。

    测试阶段非零退出是最正常不过的事 —— 有用例失败而已，判定引擎会去读 junit 报告。
    在这里把它判成故障，等于每道没修好的题都算平台出了问题。
    """
    assert classify_outcome(make_result(exit_code=1), stage=Stage.TEST) is InfraOutcome.SUCCESS


def test_ok_requires_all_three_conditions() -> None:
    assert make_result().ok
    assert not make_result(exit_code=1).ok
    assert not make_result(oom_killed=True).ok
    assert not make_result(timed_out=True).ok


# ── 环境变量白名单（§10.3「环境变量」一行）──────────────────


def test_determinism_vars_are_always_injected() -> None:
    """确定性变量一个不落。少了它们，同一个补丁两次跑可能得到不同的测试结果。"""
    env = build_env()
    for name, value in DETERMINISM_ENV.items():
        assert env[name] == value


def test_allowlisted_key_passes_through() -> None:
    env = build_env({"ANTHROPIC_API_KEY": FAKE_KEY})
    assert env["ANTHROPIC_API_KEY"] == FAKE_KEY


def test_unknown_name_raises_instead_of_being_dropped() -> None:
    """名字不在白名单里要当场报错，不能悄悄丢掉。

    悄悄丢掉的表现是几分钟后 Agent 报 401，排查时根本想不到是这一层过滤的。
    """
    with pytest.raises(EnvNotAllowedError) as exc:
        build_env({"GITHUB_TOKEN": FAKE_KEY})
    assert "GITHUB_TOKEN" in str(exc.value)


def test_determinism_vars_cannot_be_overridden() -> None:
    """确定性变量不在白名单里，所以调用方覆盖不了它们 —— 这是故意的。"""
    with pytest.raises(EnvNotAllowedError):
        build_env({"PYTHONHASHSEED": "1"})


def test_github_token_is_not_allowlisted() -> None:
    """GitHub 令牌绝不能进容器：给了它，被测 AI 就能去翻原来的修复 PR。"""
    assert "GITHUB_TOKEN" not in AGENT_ENV_ALLOWLIST


def test_proxy_names_come_in_both_cases() -> None:
    """代理变量大小写两套都要有。

    curl 只认小写的 `http_proxy`，requests / httpx 两套都认。少哪一套，
    都会有工具绕过白名单代理直连外网（E2-T4 靠这个挡 github.com）。
    """
    for lower in ("http_proxy", "https_proxy", "no_proxy"):
        assert lower in AGENT_ENV_ALLOWLIST
        assert lower.upper() in AGENT_ENV_ALLOWLIST


# ── 限额与规格 ──────────────────────────────────────────────


def test_nano_cpus_converts_fractional_cores() -> None:
    assert ResourceLimits(cpus=0.5).nano_cpus == 500_000_000
    assert ResourceLimits(cpus=2).nano_cpus == 2_000_000_000


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cpus": 0},
        {"cpus": -1},
        {"memory_mb": 4},  # docker 自己的下限是 6 MiB
        {"pids_limit": 0},
        {"tmpfs_mb": 0},
    ],
)
def test_limits_reject_nonsense(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        ResourceLimits(**kwargs)  # type: ignore[arg-type]


def test_relative_mount_source_is_rejected() -> None:
    """相对路径会被 docker 当成"卷名"，于是它默默建一个空卷挂进去，容器里是空目录，不报错。"""
    with pytest.raises(ValueError):
        BindMount(source=Path("var/workspaces/x"), target="/workspace")


def test_workspace_mount_uses_the_fixed_target() -> None:
    """工作区在容器里的路径是固定的，题目里的 test_command 才不用关心宿主机目录结构。"""
    assert BindMount.workspace(Path("/tmp/ws")).target == WORKSPACE_TARGET


@pytest.mark.parametrize("kwargs", [{"timeout_s": 0}, {"command": []}, {"stop_grace_s": -1}])
def test_spec_rejects_nonsense(kwargs: dict[str, object]) -> None:
    base: dict[str, object] = {"image": "x", "command": ["true"], "timeout_s": 10}
    base.update(kwargs)
    with pytest.raises(ValueError):
        ContainerSpec(**base)  # type: ignore[arg-type]


def test_create_kwargs_carry_every_hardening_flag() -> None:
    """加固项全都要真的传给 docker。

    这一条盯的是"参数写了但没生效"：字段名拼错、或者哪次重构漏掉一行，
    容器照样能跑起来，只是不再受限 —— 不会有任何报错。
    限额有没有真的落到内核，由 test_container.py 的 cgroup 用例负责。
    """
    spec = ContainerSpec(
        image="python:3.11-slim",
        command=["true"],
        timeout_s=10,
        limits=ResourceLimits(cpus=0.5, memory_mb=256, pids_limit=32, tmpfs_mb=64),
        network=NetworkMode.NONE,
        mounts=(BindMount(source=Path("/tmp/ws"), target="/workspace"),),
        workdir="/workspace",
    )
    kwargs = _create_kwargs(spec)

    assert kwargs["nano_cpus"] == 500_000_000
    assert kwargs["mem_limit"] == "256m"
    # swap 必须等于内存上限，否则超内存的题不会被 OOM 杀掉，而是慢到超时
    assert kwargs["memswap_limit"] == kwargs["mem_limit"]
    assert kwargs["pids_limit"] == 32
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["network_mode"] == "none"
    assert kwargs["tty"] is False
    assert "size=64m" in kwargs["tmpfs"]["/tmp"]
    assert kwargs["volumes"]["/tmp/ws"] == {"bind": "/workspace", "mode": "rw"}
    assert kwargs["working_dir"] == "/workspace"


def test_create_kwargs_omit_workdir_when_unset() -> None:
    """没指定工作目录就不传。

    传一个镜像里不存在的目录，容器会直接起不来，报的是 "no such file or directory"，
    看起来像命令找不到。
    """
    spec = ContainerSpec(image="python:3.11-slim", command=["true"], timeout_s=10)
    assert "working_dir" not in _create_kwargs(spec)


def test_read_only_mount_is_marked_ro() -> None:
    spec = ContainerSpec(
        image="x",
        command=["true"],
        timeout_s=10,
        mounts=(BindMount(source=Path("/tmp/ws"), target="/workspace", read_only=True),),
    )
    assert _create_kwargs(spec)["volumes"]["/tmp/ws"]["mode"] == "ro"


def test_labels_mark_the_container_as_ours() -> None:
    """标签是孤儿回收的唯一凭据，run_id 也一起写进去，方便把残留容器对回具体哪次运行。"""
    spec = ContainerSpec(
        image="x", command=["true"], timeout_s=10, stage=Stage.AGENT, run_id="run-7"
    )
    labels = _create_kwargs(spec)["labels"]
    assert labels["bench.owner"] == "coding-agent-benchmark"
    assert labels["bench.stage"] == "AGENT"
    assert labels["bench.run_id"] == "run-7"


def test_container_user_is_never_root() -> None:
    """容器里不能是 root（§10.3「文件系统策略」一行）。"""
    assert not default_container_user().startswith("0:")


# ── 日志截断与时间戳 ────────────────────────────────────────


def test_read_capped_keeps_everything_under_the_limit() -> None:
    text, truncated = _read_capped([b"abc", b"de"], 10)
    assert (text, truncated) == ("abcde", False)


def test_read_capped_cuts_at_the_limit() -> None:
    """跑飞的 Agent 能刷出几个 GB，全读进内存会把 Worker 撑爆。"""
    text, truncated = _read_capped([b"a" * 6, b"b" * 6], 8)
    assert text == "a" * 6 + "b" * 2
    assert truncated


def test_read_capped_survives_invalid_utf8() -> None:
    """容器可以输出任意字节。解码报错会让整次评测失败，而我们要的只是把日志存下来。"""
    text, _ = _read_capped([b"\xff\xfe"], 10)
    assert text  # 具体替换成什么字符不重要，不炸就行


def test_parse_docker_time_handles_nanoseconds() -> None:
    """docker 给的是纳秒精度，而 `datetime.fromisoformat` 只认 3 位或 6 位小数。

    不截断的话它直接抛 ValueError，孤儿回收会在算年龄那一步整个挂掉。
    """
    parsed = parse_docker_time("2026-09-04T14:21:33.123456789Z")
    assert parsed == datetime(2026, 9, 4, 14, 21, 33, 123456, tzinfo=UTC)


@pytest.mark.parametrize("raw", ["2026-09-04T14:21:33Z", "2026-09-04T14:21:33.123Z"])
def test_parse_docker_time_handles_shorter_forms(raw: str) -> None:
    parsed = parse_docker_time(raw)
    assert parsed is not None and parsed.tzinfo is not None


@pytest.mark.parametrize("raw", ["", "   ", "not-a-time"])
def test_parse_docker_time_returns_none_on_garbage(raw: str) -> None:
    assert parse_docker_time(raw) is None
