"""跑一轮测试要知道的全部信息（E4-T2）。

## 为什么不直接传 `TaskDefinition`

模块边界不允许 —— `app.evaluation` 和 `app.benchmark` 在 import-linter 的分层里是
并排的（`app.evaluation | app.benchmark | app.report`），并排就是**互不可见**。

这不是绕开约束的小聪明，是仓库里已有的做法：`app.sandbox.container.ResourceLimits`
出于同样的理由收三个数而不是收一个 `TaskDefinition`，映射由上层调用方做。

放在 `app.domain` 是因为它谁都能看见：`app.benchmark` 用它当出口
（`TaskDefinition.execution_plan()`），`app.evaluation` 用它当入口。
映射只有那一处，不会两边各写一份然后慢慢漂。

## 它不是 TaskDefinition 的子集

少了 `issue_body`、`gold_patch`、`hints_text` 这些 —— 测试执行器**不该**看见它们。
`gold_patch` 尤其：它是官方答案，跟"跑一轮测试"这件事没有任何关系，
传进来只会多一条泄漏路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """一道题跑测试所需的全部输入。纯数据，没有行为。"""

    #: 代码回退到哪个提交（"bug 还在"的状态）。
    base_commit: str
    #: 仅含测试文件的官方 diff，由平台施加，不下发给被测 AI。
    test_patch: str
    #: `test_patch` 实际改动的路径。**必须**并进受保护清单（C-42 最后一条、C-75）——
    #: 有些题目的测试改动会带上名字完全不像测试的 fixture 文件，靠通配符匹配不到。
    test_patch_paths: tuple[str, ...]
    #: 修好之后必须由失败变通过的用例。
    fail_to_pass: tuple[str, ...]
    #: 修复前后都必须通过的用例（回归检查范围）。
    pass_to_pass: tuple[str, ...]
    #: 跑测试的命令，会被 `shlex.split` 之后接上用例 ID 列表。
    test_command: str
    #: 测试报告在仓库里的相对路径，例如 `report/junit.xml`。
    test_report_path: str
    #: 跑测试前要先执行的命令（就地编译扩展之类），大多数题目为 None。
    pre_test_command: str | None = None
    #: 环境规格上额外声明的受保护路径。
    extra_protected_paths: tuple[str, ...] = ()
    test_timeout_s: int = 480
    sandbox_cpu: float = 1.0
    sandbox_memory_mb: int = 1536
    sandbox_pids_limit: int = 512
    #: 只用来打日志，让报错能对回是哪道题。
    task_id: str = ""
    #: 保留给以后按题覆盖镜像用；现在由调用方直接给 `execute_tests(image=...)`。
    image: str | None = field(default=None)

    @property
    def test_ids(self) -> tuple[str, ...]:
        """要跑哪些用例：F2P + P2P，去重保序（C-17 只跑子集）。

        去重是必须的：同一条用例同时出现在两个名单里的话，pytest 会跑两遍，
        junitxml 里就有两条同名 testcase。解析器按"先来后到"取第一条，结果是确定的，
        但报告里凭空多一条，看的人会以为哪里错了。
        """
        seen: dict[str, None] = {}
        for test_id in (*self.fail_to_pass, *self.pass_to_pass):
            seen.setdefault(test_id)
        return tuple(seen)


__all__ = ["ExecutionPlan"]
