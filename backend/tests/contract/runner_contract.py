"""适配器契约测试套件（E3-T1，`04-runner-protocol.md` §9.3 的六条）。

**这个文件不是测试，是给别人继承的模板。** 文件名故意不叫 `test_*.py`，
pytest 不会直接收集它——直接收集的话，这里的六条会在没有适配器的情况下跑一遍然后报错。

接一个新适配器，写三行就能把六条全跑上：

    from tests.contract.runner_contract import AgentRunnerContract

    class TestNoopRunner(AgentRunnerContract):
        produces_patch = False              # Noop 交空补丁，这是它的定义

        @pytest.fixture
        def runner(self) -> AgentRunner:
            return NoopRunner()

## 六条分别在防什么

| # | 用例 | 防的是 |
|:-:|:---|:---|
| 1 | `test_probe_*` | 一个过期的 Key 让几百次评测全挂，而这本该第一秒就发现 |
| 2 | `test_produces_patch_*` | 适配器跑完了却没把补丁交出来 |
| 3 | `test_deadline_*` | 超时之后适配器不回来，或者留下孤儿进程 |
| 4 | `test_protected_path_*` | 适配器自作主张过滤受保护路径，把作弊证据擦掉 |
| 5 | `test_cost_*` | 报不出成本时填 0，让成本统计悄悄偏低 |
| 6 | `test_result_survives_noise` | 结果过不了 stdin/stdout 这条通道 |

## 第 4 条为什么反着测

直觉是"适配器应该把受保护路径的改动剔掉"。**不对**。

协议 C-08b 要求记录 `protected_path_edit_attempted`——AI 有没有试图改测试文件
是一个要留证的行为。适配器要是自己先过滤了，这个证据就没了，
`PatchKind.AGENT_RAW` 和 `AGENT_NORMALIZED` 存两份也就失去意义。

所以契约是：**适配器交原始 diff，过滤由平台做**（C-41，E3-T3）。
第 4 条同时验两件事：原始补丁里那条改动还在，以及平台的规则确实会把它认出来。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.domain.enums import CostSource, IssueLanguage
from app.domain.patch_paths import derive_patch_paths
from app.domain.protected_paths import agent_visible_patterns, enforcement_patterns, protected_hits
from app.runner.protocol import (
    AgentConfig,
    AgentRunner,
    AgentRunResult,
    AgentTaskInput,
    Constraints,
    IssueInput,
    ModelInput,
    RepoInput,
    parse_result_stdout,
)

#: 第 3 条给适配器的默认收尾时间。给得宽一点：这条测的是"它到底回不回来"，
#: 不是"它有多快"。卡太紧会在机器负载高时随机变红。
#: 子类可以通过 `deadline_grace_s` 调整——起容器的适配器收尾本来就慢一些。
DEADLINE_GRACE_S = 30.0

#: 造任务输入用的假 base commit。40 位十六进制，内容本身无所谓。
SAMPLE_BASE_COMMIT = "0" * 40

#: 第 4 条要改的那个受保护文件。选 `tests/test_sample.py` 是因为它命中默认清单里
#: 最普通的一条 `tests/**`，不依赖任何题目特有的规则。
PROTECTED_TARGET = "tests/test_sample.py"


def make_task_input(*, task_id: str = "bench-contract__demo-1", deadline_ms: int) -> AgentTaskInput:
    """造一份合规的任务输入。

    `protected_paths` 用 `agent_visible_patterns()`——**不是** `enforcement_patterns()`。
    后者含该题的 `test_patch_paths`，下发给 AI 就是泄题（协议 C-76）。
    这里用对了，契约套件本身才不会给适配器做坏示范。
    """
    return AgentTaskInput(
        task_id=task_id,
        issue=IssueInput(
            title="登录时空密码不应通过校验",
            body="调用 login(user, '') 时会返回 True，空密码直接放行。期望空密码一律拒绝。",
            language=IssueLanguage.ZH,
        ),
        repo=RepoInput(name="example/demo", base_commit=SAMPLE_BASE_COMMIT),
        constraints=Constraints(
            deadline_unix_ms=deadline_ms,
            protected_paths=list(agent_visible_patterns()),
        ),
        model=ModelInput(name="contract-fake-model"),
    )


def child_pids() -> set[int]:
    """当前进程的直接子进程 pid。拿不到（比如没有 /proc）就返回空集合。

    第 3 条靠它查孤儿进程：适配器起了子进程去跑 CLI，超时后没收干净的话，
    这些进程会一直挂着占内存，而症状要到几十道题之后才以"机器变慢"的形式出现。
    """
    task_dir = Path("/proc/self/task")
    if not task_dir.is_dir():
        return set()
    pids: set[int] = set()
    for task in task_dir.iterdir():
        try:
            pids.update(int(p) for p in (task / "children").read_text().split())
        except OSError:
            continue  # 线程在遍历过程中退出了，跳过
    return pids


class AgentRunnerContract:
    """六条契约。子类给一个 `runner` fixture 就能全跑起来。"""

    #: 截止时刻已过之后，留给这个适配器收尾的秒数。
    deadline_grace_s: float = DEADLINE_GRACE_S

    #: 正常那几条给适配器的墙钟预算（秒）。
    #: 假适配器一瞬间就返回，60 秒绰绰有余；真实 CLI 要起容器、装载自己、
    #: 扫一遍仓库、再等模型回话，60 秒多半只够走到一半，于是第 2 条会因为超时
    #: 拿到空补丁而变红 —— 那测出来的是预算不够，不是适配器有问题。
    task_deadline_s: float = 60.0

    #: 这个适配器在能解的题上会不会交出非空补丁。
    #: **Noop 哨兵要设成 False**——交空补丁是它的定义，不是缺陷。
    #: 真实适配器设成 False 是一个危险信号，那意味着它从来没产出过东西。
    produces_patch: bool = True

    # ── 子类必须提供 ────────────────────────────────────────

    @pytest.fixture
    def runner(self) -> AgentRunner:
        raise NotImplementedError("子类要提供一个 runner fixture")

    @pytest.fixture
    def config(self) -> AgentConfig:
        """适配器的 harness 侧配置。默认空，子类按需覆盖。"""
        return AgentConfig()

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        """给适配器干活的目录。

        默认是个空目录。真实适配器的子类应该覆盖成 `materialize_workspace()`
        物化出来的 `Workspace`——`run()` 的第二个参数在协议里标的是 `Any`，
        正是为了让假适配器不必先造一个真工作区。
        """
        return tmp_path

    # ── 子类可选提供 ────────────────────────────────────────

    def make_task(self, *, deadline_ms: int) -> AgentTaskInput:
        """这几条契约用的任务输入。默认是套件自带的那份合成任务。

        **真实适配器的子类应该覆盖成一道真题。** 合成任务说的仓库是
        `example/demo`，题干说的函数是 `login()`；而 `workspace` fixture 物化出来的
        是另一个仓库。假适配器不看这些，真实 CLI 会照着题干去仓库里找，找不到就
        什么都不改，于是第 2 条以"没产出补丁"变红 —— 那测出来的是任务和工作区
        对不上，不是适配器有毛病。
        """
        return make_task_input(deadline_ms=deadline_ms)

    def runner_that_edits(self, relative_path: str) -> AgentRunner | None:
        """造一个"会去改指定文件"的同类适配器，用于第 4 条。

        给不出就返回 None，第 4 条跳过并记下原因。真实 CLI 适配器多半给不出——
        没法命令 Aider 去改某个具体文件；Mock / Oracle 这类可编程的假适配器给得出。
        """
        return None

    # ── 六条契约 ────────────────────────────────────────────

    def _deadline_ms(self) -> int:
        """正常那几条用的截止时刻。子类调 `task_deadline_s` 就能整体放宽。"""
        return int(time.time() * 1000) + int(self.task_deadline_s * 1000)

    def test_probe_reports_availability(self, runner: AgentRunner) -> None:
        """第 1 条：探活能给出结论，失败时必须说清原因。

        实验开跑前对每个适配器探一次。不探的话，一个过期的 API Key 会让几百次评测
        全部以 AGENT_AUTH_ERROR 失败，而这本来在第一秒就能发现。

        探活失败本身不算违约（Key 没配是环境问题），**不说原因才算**：
        一个只返回 False 的 probe 等于没有 probe。
        """
        probe = runner.probe()
        assert isinstance(probe.ok, bool)
        if not probe.ok:
            assert probe.detail.strip(), "探活失败却没给原因，排查时无从下手"

    def test_produces_patch_on_a_solvable_task(
        self, runner: AgentRunner, workspace: Path, config: AgentConfig
    ) -> None:
        """第 2 条：跑一道能解的题，交出符合声明的补丁。"""
        task = self.make_task(deadline_ms=self._deadline_ms())
        result = runner.run(task, workspace, config)

        assert isinstance(result, AgentRunResult)
        assert result.agent_name, "结果里必须带适配器名字，否则排行榜对不上号"
        assert result.has_patch is self.produces_patch
        if result.has_patch:
            # 补丁得是能解析出路径的 diff，不能是一段自然语言描述
            assert derive_patch_paths(result.patch), "补丁解析不出任何路径，不是合法的 diff"

    def test_returns_gracefully_when_the_deadline_has_passed(
        self, runner: AgentRunner, workspace: Path, config: AgentConfig
    ) -> None:
        """第 3 条：截止时刻已过时按时收手，不留孤儿进程。

        deadline 传一个**已经过去**的时刻。适配器应当尽快返回一个合法结果，
        而不是抛异常、也不是继续干满全程。

        墙钟硬超时不靠这一条兜底——那是 harness 用 `docker stop` 强杀的事（§9.6）。
        这里测的是软预算：适配器认不认这个字段。
        """
        before = child_pids()
        task = self.make_task(deadline_ms=int(time.time() * 1000) - 1_000)

        started = time.monotonic()
        result = runner.run(task, workspace, config)
        elapsed = time.monotonic() - started

        assert isinstance(result, AgentRunResult)
        assert elapsed < self.deadline_grace_s, f"截止已过还跑了 {elapsed:.1f} 秒"
        leaked = child_pids() - before
        assert not leaked, f"留下了孤儿进程：{sorted(leaked)}"

    def test_protected_path_edits_stay_in_the_raw_patch(
        self, workspace: Path, config: AgentConfig
    ) -> None:
        """第 4 条：受保护路径的改动**留在原始补丁里**，由平台去剔除。

        方向和直觉相反，理由见模块开头。一句话：适配器自己先过滤掉，
        `protected_path_edit_attempted` 这个证据就没了（协议 C-08b）。
        """
        runner = self.runner_that_edits(PROTECTED_TARGET)
        if runner is None:
            pytest.skip("这个适配器没法被指定去改某个文件，第 4 条跳过")

        task = self.make_task(deadline_ms=self._deadline_ms())
        result = runner.run(task, workspace, config)

        paths = derive_patch_paths(result.patch)
        assert PROTECTED_TARGET in paths, "适配器把受保护路径的改动自己过滤掉了，证据丢了"
        # 平台这边必须认得出它 —— 认不出的话，第一道防线（C-41）就是空的
        assert protected_hits(tuple(paths), enforcement_patterns())

    def test_cost_is_reported_or_explicitly_unavailable(
        self, runner: AgentRunner, workspace: Path, config: AgentConfig
    ) -> None:
        """第 5 条：成本要么给数字，要么明说"拿不到"。

        订阅制 CLI 报不出金额是常态，那就标 `unavailable`，平台按 token 用量估算
        并在报告里标成 `estimated`（协议纪律 3）。**不能填 0** —— 0 和"不知道"
        是两回事，填 0 会让成本统计悄悄偏低，而且没人看得出来。
        """
        task = self.make_task(deadline_ms=self._deadline_ms())
        result = runner.run(task, workspace, config)

        assert isinstance(result.cost_source, CostSource)
        if result.cost_source is CostSource.UNAVAILABLE:
            assert result.cost_usd is None
        else:
            assert result.cost_usd is not None and result.cost_usd >= 0
        if result.token_usage is not None:
            usage = result.token_usage
            assert usage.total >= max(usage.input, usage.output)

    def test_result_survives_noisy_stdout(
        self, runner: AgentRunner, workspace: Path, config: AgentConfig
    ) -> None:
        """第 6 条：结果能原样过一遍 stdin/stdout 这条通道。

        把适配器返回的结果序列化，埋进几百行日志里，再按协议纪律 1 读回来，
        必须和原来相等。真实 CLI 会刷屏、会打进度条，这条通道是它们的必经之路。

        埋的噪声里放了两样真会出事的东西：一行**看起来像 JSON 的日志**
        （只认最后一行的规则要顶得住），和一个 `\\r` 进度条（最后一行会变成
        `进度\\r{...}`）。
        """
        task = self.make_task(deadline_ms=self._deadline_ms())
        result = runner.run(task, workspace, config)

        result_line = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
        noisy = "\n".join(
            [
                *[f"[info] 第 {i} 步：读文件" for i in range(200)],
                '{"note": "这行长得像结果，但它不是最后一行"}',
                "下载中 40%\r下载中 80%\r下载完成",
                result_line,
                "",  # 几乎所有 CLI 都会多打一个换行
            ]
        )
        assert parse_result_stdout(noisy) == result
