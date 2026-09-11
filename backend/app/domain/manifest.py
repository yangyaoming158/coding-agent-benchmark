"""运行 Manifest 的词汇表（E5-T4）。

`evaluation_runs.manifest` 这一列装的是"这次运行的可复现性清单"：镜像 digest 表、
数据集哈希、harness 的 git sha、Agent 版本与参数、确定性环境变量、并发限额。
装配它的代码在 `app.evaluation.manifest`，这里只放**两边都要认的那几个名字**。

为什么名字要单独放在 `domain`：`app.benchmark` 和 `app.evaluation` 在 import-linter
的契约里是互不可见的两个模块（`AGENTS.md` 第 8 节），而数据集摘要那个键两边都要写 ——
`app.benchmark.dataset` 发布门禁时写，`app.evaluation.manifest` 建实验时写。
两边各写一个字符串字面量的话，哪天改名就会有一边没跟上，而错的那一边**不报错**，
只是查不到门禁记录。
"""

#: manifest 的结构版本。结构变了就加 1，好让老运行能被认出来。
MANIFEST_VERSION = "1.0"

#: 数据集快照摘要在 manifest 里的键。E1-T6 定的口径，位置和名字都不动 ——
#: `app.benchmark.dataset.gate_verdict()` 靠这个 JSONB 路径找门禁实验。
DATASET_SNAPSHOT_DIGEST_KEY = "dataset_snapshot_digest"

#: **允许两次运行之间不同**的顶层键。
#:
#: 这个集合就是任务卡那句"两次运行的 manifest diff 只在时间戳上不同"的机器化定义：
#: 集合之外的键必须逐字相同，集合之内的如实记录但不参与等价判断。
#:
#: 为什么必须有这个集合：NFR-02 要的是"**异机**异时复现"。跑在第二台机器上时
#: `host` 一定不同，`created_at` 一定不同 —— 不把它们划出去，这条验收标准
#: 在第二台机器上永远过不了；而不记 `host` 的话，两次结果对不上时
#: 第一个要问的问题（"是不是换机器了"）就没有证据可查。
VOLATILE_KEYS = frozenset(
    {
        #: 这份 manifest 什么时候拼出来的。
        "created_at",
        #: 跑在什么机器上（docker 版本、内核、CPU 数、内存）。
        "host",
        #: 这次是重放哪一次运行（`cli.experiment replay` 写）。
        "replay_of",
    }
)

__all__ = ["DATASET_SNAPSHOT_DIGEST_KEY", "MANIFEST_VERSION", "VOLATILE_KEYS"]
