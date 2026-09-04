"""三个哨兵适配器：Oracle / Noop / Mock（E3-T2）。

它们**不调用任何外部服务**，所以在没有大模型额度、没有网络、甚至没有 Docker 的
情况下，整条评测链都能自测。这是它们存在的全部理由。

    from app.runner.adapters import MockBehavior, MockRunner, NoopRunner, OracleRunner

| 适配器 | 交什么 | 在健康题库上的解决率 |
|:---|:---|:---|
| `OracleRunner` | 官方补丁 | 必须 **100%** |
| `NoopRunner` | 空补丁 | 必须 **0%** |
| `MockRunner` | 按配置来，六选一 | 看配的是哪种行为 |

前两个是题库发布的硬门槛（协议 C-50）：Oracle 不到 100% 说明有坏题或者判定引擎
有 bug，Noop 不是 0% 说明有的题在修复前测试就已经通过了。

## 三个共同点

1. **不碰工作区**。它们直接在结果里交出补丁字符串，不去改 `workspace` 里的文件。
   真实适配器走的是另一条路（改文件 → `git diff` → patch），两条路最后都汇到
   `AgentRunResult.patch`，下游看到的东西一样。
2. **成本如实报 0**（`cost_source=reported`、`cost_usd=0.0`）。协议纪律 3 说的
   "不要填 0"针对的是**拿不到**成本的情况；哨兵的成本是真的 0，这是报得出来的 0，
   不是"不知道"。
3. **官方补丁靠外部注入**，不自己去读题库。`app.runner` 在分层里压在
   `app.benchmark` 下面，import 不到 `TaskDefinition`；而且编排层本来就已经
   把题读出来了，再读一遍等于两份数据源。

## 谁来构造它们

数据库 `agents.adapter_class` 里存的就是这三个类的完整路径（`cli/seed.py`）：

    app.runner.adapters.oracle.OracleRunner
    app.runner.adapters.noop.NoopRunner
    app.runner.adapters.mock.MockRunner

编排层（E5）按这个字符串反射出类，再把官方补丁和行为配置喂进去。
"""

from app.runner.adapters.base import PatchLookup, PatchSource, as_lookup, sentinel_result
from app.runner.adapters.mock import MockBehavior, MockRunner
from app.runner.adapters.noop import NoopRunner
from app.runner.adapters.oracle import OracleRunner

__all__ = [
    "MockBehavior",
    "MockRunner",
    "NoopRunner",
    "OracleRunner",
    "PatchLookup",
    "PatchSource",
    "as_lookup",
    "sentinel_result",
]
