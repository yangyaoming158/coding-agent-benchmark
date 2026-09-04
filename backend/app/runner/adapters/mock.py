"""Mock 哨兵：六种行为按配置精确触发（E3-T2）。

Oracle 和 Noop 各自只有一种行为，它们探的是解决率的上下界。Mock 补的是中间那一大片：
**判定链上每一条失败路径都得有人走一遍**，而真实 Agent 什么时候交出什么样的补丁
是没法点菜的。

| 行为 | 交出什么 | 判定链上应该落到哪 |
|:---|:---|:---|
| `correct_patch` | 官方补丁 | `RESOLVED` |
| `wrong_patch` | 能打上、但修不好的补丁 | `UNRESOLVED` |
| `empty_patch` | 空补丁 | `EMPTY_PATCH` |
| `timeout` | 磨蹭过截止时刻，空手而归 | `AGENT_TIMEOUT` |
| `malformed_patch` | 非空但 `git apply` 读不下去 | `PATCH_APPLY_FAILED` / `INVALID_PATCH` |
| `protected_path_edit` | 改了 `tests/` 下的文件 | `protected_path_edit_attempted = true` |

最后一列是**期望**，不是这个模块的保证——判定由 E4-T3 做，Mock 只负责把输入造出来。

## 补丁的形状为什么都是"新建文件"

除了 `correct_patch` 用真的官方补丁，其余几种造出来的 diff 都是新建文件。
理由很实际：新建文件的 diff 不引用仓库里任何一行现有内容，所以在**任何**工作区上
都能干净地 `git apply`。要是造成"修改某个已有文件"，就得为每道题挑一个真实存在的
文件、抄一段真实的上下文，换一道题就得换一份补丁。

`malformed_patch` 是唯一的例外，它必须打不上——用的办法是把 hunk 头里的行数写错，
这同样只取决于补丁自己，和工作区里有什么无关。

## 改受保护文件的那份补丁里有两个文件

一个普通源文件加一个 `tests/` 下的新测试。真实世界里"AI 试图改测试"通常伴随着
它对源码的真实改动，只造后者的话，E3-T3 的归一化就没有"该留的留下、该剔的剔掉"
可测了。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from app.runner.adapters.base import (
    GOLD_PATCH_MISSING,
    PatchSource,
    as_lookup,
    sentinel_result,
)
from app.runner.protocol import (
    AgentConfig,
    AgentError,
    AgentRunResult,
    AgentTaskInput,
    ProbeResult,
)


class MockBehavior(StrEnum):
    """Mock 能触发的六种行为。

    这不是领域枚举，不入库成一列——它的值存在 `agent_configs.params` 这个 JSON 里，
    所以取值用小写，和 params 里其他键的风格一致。
    """

    CORRECT_PATCH = "correct_patch"
    WRONG_PATCH = "wrong_patch"
    EMPTY_PATCH = "empty_patch"
    TIMEOUT = "timeout"
    MALFORMED_PATCH = "malformed_patch"
    PROTECTED_PATH_EDIT = "protected_path_edit"


#: Mock 假装写出来的那个源文件。名字带 `mock_agent` 前缀，一眼看得出是假 Agent 留的痕迹。
MOCK_SOURCE_PATH = "mock_agent_attempt.py"

#: `protected_path_edit` 默认新建的测试文件。走 `tests/**` 这条最普通的受保护规则，
#: 不依赖任何一道题特有的 `test_patch_paths`。
DEFAULT_PROTECTED_TARGET = "tests/test_mock_agent.py"

#: `timeout` 行为最多真的睡多久（秒）。
#:
#: 上限不能少：golden 题的 `agent_timeout_s` 是 300，真按 deadline 睡满，
#: 一条测试就要跑五分钟。默认值只要够让墙钟走过一个近距离的 deadline 就行。
#: 想测 harness 的墙钟强杀（§9.6），把它调大，或者把 deadline 调近。
DEFAULT_MAX_SLEEP_S = 0.05

#: `timeout` 行为报的错误码。
DEADLINE_EXCEEDED = "deadline_exceeded"


def new_file_diff(path: str, lines: Sequence[str]) -> str:
    """造一段"新建文件"的 unified diff。

    没有 `index` 行：`git apply`（不带 `--index`）用不到它，而随手编一个假的
    blob 哈希，将来换成 `git apply --index` 就会莫名其妙地失败。
    """
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n" + body
    )


def malformed_diff(path: str) -> str:
    """造一段 `git apply` 读不下去的 diff。

    手法是把 hunk 头的行数写错：声称新增 3 行，实际只给了 1 行。
    `git apply` 会以 `corrupt patch at line N` 拒收。

    协议里 `INVALID_PATCH` 的定义是"补丁非空但打不上去"（第 109 行那张表），
    所以它**必须仍然是一段解析得出路径的 diff**，不能拿一整段自然语言冒充——
    那种输入走的是另一条路（适配器根本没交出补丁）。
    """
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1,3 @@\n"
        "+def mock_fix():\n"
    )


def wrong_patch_diff() -> str:
    """能干净打上、但一行都改不动结果的补丁。

    新建一个孤立的模块，谁都不 import 它。F2P 该挂还是挂，判定结论是 `UNRESOLVED`——
    这正是"AI 交了东西但没修好"最常见的样子。
    """
    return new_file_diff(
        MOCK_SOURCE_PATH,
        [
            '"""Mock 哨兵留下的假修复：能打上，但和 bug 没关系。"""',
            "",
            "",
            "def mock_agent_attempt() -> None:",
            "    return None",
        ],
    )


def protected_path_edit_diff(protected_target: str) -> str:
    """一半是真改动，一半是改测试文件。

    改测试的那一半**必须留在原始补丁里**，不能由适配器自己过滤掉（协议 C-08b、
    契约第 4 条）。过滤掉的话，`protected_path_edit_attempted` 就没有证据了。
    过滤是平台在 E3-T3 做的事。
    """
    source = new_file_diff(
        MOCK_SOURCE_PATH,
        [
            '"""Mock 哨兵留下的假修复。"""',
            "",
            "",
            "def mock_agent_attempt() -> None:",
            "    return None",
        ],
    )
    cheat = new_file_diff(
        protected_target,
        [
            "# Mock 哨兵新建的测试：不管源码对不对，它都通过。",
            "def test_mock_agent_always_passes() -> None:",
            "    assert True",
        ],
    )
    return source + cheat


class MockRunner:
    """行为可编程的假 Agent。

    最简单的用法是全局一种行为：

        runner = MockRunner(MockBehavior.WRONG_PATCH)

    要在一次实验里凑出混合结果（这才是判定链和报表真正需要的输入），
    用 `per_task` 逐题指定，没指定的题走默认行为：

        runner = MockRunner(
            MockBehavior.CORRECT_PATCH,
            patches={task.task_id: task.gold_patch for task in tasks},
            per_task={"acme__web-42": MockBehavior.TIMEOUT},
        )

    编排层从数据库 `agent_configs.params` 里读配置，走 `from_params()`。
    """

    name = "mock"

    def __init__(
        self,
        behavior: MockBehavior = MockBehavior.CORRECT_PATCH,
        *,
        patches: PatchSource | None = None,
        per_task: Mapping[str, MockBehavior] | None = None,
        max_sleep_s: float = DEFAULT_MAX_SLEEP_S,
        protected_target: str = DEFAULT_PROTECTED_TARGET,
    ) -> None:
        self.behavior = behavior
        self.per_task: dict[str, MockBehavior] = dict(per_task or {})
        self.max_sleep_s = max_sleep_s
        self.protected_target = protected_target
        self._lookup = as_lookup(patches)

    @classmethod
    def from_params(cls, params: Mapping[str, Any], *, patches: PatchSource | None = None) -> Self:
        """从 `agent_configs.params` 那个 JSON 里构造。

        认得的键：`behavior`、`per_task`、`max_sleep_s`、`protected_target`。
        行为名写错就抛 `ValueError`，不静默退回默认值——静默退回的话，
        一个拼错的 `"timout"` 会让整批实验安安静静地跑成"正确补丁"，
        而结果看上去完全正常。
        """
        behavior = _parse_behavior(params.get("behavior", MockBehavior.CORRECT_PATCH))
        raw_per_task = params.get("per_task") or {}
        if not isinstance(raw_per_task, Mapping):
            raise ValueError(f"per_task 必须是 task_id → 行为名 的映射，实际是 {raw_per_task!r}")
        return cls(
            behavior,
            patches=patches,
            per_task={str(k): _parse_behavior(v) for k, v in raw_per_task.items()},
            max_sleep_s=float(params.get("max_sleep_s", DEFAULT_MAX_SLEEP_S)),
            protected_target=str(params.get("protected_target", DEFAULT_PROTECTED_TARGET)),
        )

    def behavior_for(self, task_id: str) -> MockBehavior:
        """这道题该走哪种行为。逐题配置优先于全局默认。"""
        return self.per_task.get(task_id, self.behavior)

    def probe(self) -> ProbeResult:
        """永远可用：不依赖任何外部东西。"""
        return ProbeResult(
            ok=True, agent_version="1.0", detail=f"Mock 哨兵，默认行为 {self.behavior.value}"
        )

    def run(self, task: AgentTaskInput, workspace: object, config: AgentConfig) -> AgentRunResult:
        """按配好的行为跑一遍。

        除了 `timeout`，其余五种行为**都不看 deadline**：配了什么就交什么。
        Mock 的价值在于"要什么给什么"，让它随时钟改变输出，测试就会时不时变红。
        """
        started_at = datetime.now(UTC)
        behavior = self.behavior_for(task.task_id)

        if behavior is MockBehavior.TIMEOUT:
            return self._timeout(task, started_at)
        if behavior is MockBehavior.EMPTY_PATCH:
            return sentinel_result(agent_name=self.name, started_at=started_at)
        if behavior is MockBehavior.CORRECT_PATCH:
            return self._correct_patch(task, started_at)

        patch = {
            MockBehavior.WRONG_PATCH: wrong_patch_diff(),
            MockBehavior.MALFORMED_PATCH: malformed_diff(MOCK_SOURCE_PATH),
            MockBehavior.PROTECTED_PATH_EDIT: protected_path_edit_diff(self.protected_target),
        }[behavior]
        return sentinel_result(agent_name=self.name, started_at=started_at, patch=patch)

    # ── 单独拿出来的两种 ────────────────────────────────────

    def _correct_patch(self, task: AgentTaskInput, started_at: datetime) -> AgentRunResult:
        """交官方补丁。没喂补丁进来就明着报错，不悄悄交空手。

        悄悄交空手的后果和 Oracle 那边一样：这道题被判成 `UNRESOLVED`，
        排查的人会去翻判定引擎，而真正的原因（补丁没喂进来）在那里看不出来。
        """
        patch = self._lookup(task.task_id)
        if patch is None:
            return sentinel_result(
                agent_name=self.name,
                started_at=started_at,
                exit_code=1,
                error=AgentError(
                    code=GOLD_PATCH_MISSING,
                    message=f"correct_patch 行为要有 {task.task_id} 的官方补丁，但没喂进来",
                ),
            )
        return sentinel_result(agent_name=self.name, started_at=started_at, patch=patch)

    def _timeout(self, task: AgentTaskInput, started_at: datetime) -> AgentRunResult:
        """磨蹭到截止时刻，然后空手而归。

        真睡的时长是 `min(离截止还有多久, max_sleep_s)`：deadline 已经过了就一秒不睡，
        契约第 3 条（截止已过要按时收手）才过得去。
        """
        sleep_s = min(task.constraints.remaining_ms() / 1000, self.max_sleep_s)
        if sleep_s > 0:
            time.sleep(sleep_s)
        return sentinel_result(
            agent_name=self.name,
            started_at=started_at,
            error=AgentError(
                code=DEADLINE_EXCEEDED,
                message="磨蹭到了截止时刻，什么都没交出来",
            ),
        )


def _parse_behavior(value: object) -> MockBehavior:
    """把配置里的行为名转成枚举。认不出来就报错，并列出所有合法取值。"""
    if isinstance(value, MockBehavior):
        return value
    try:
        return MockBehavior(str(value))
    except ValueError as exc:
        legal = "、".join(item.value for item in MockBehavior)
        raise ValueError(f"不认识的 Mock 行为 {value!r}，合法取值：{legal}") from exc


__all__ = [
    "DEADLINE_EXCEEDED",
    "DEFAULT_MAX_SLEEP_S",
    "DEFAULT_PROTECTED_TARGET",
    "MOCK_SOURCE_PATH",
    "MockBehavior",
    "MockRunner",
    "malformed_diff",
    "new_file_diff",
    "protected_path_edit_diff",
    "wrong_patch_diff",
]
