"""穷举 lifecycle_status × infra_outcome × agent_outcome 的全部组合，
逐格判定合法性，并检查有没有空洞（某个取值到不了）或重叠（同一情况有两种解释）。

规则来源：docs/evaluation-protocol.md 的 C-04a、C-08、C-09、C-18、C-20、C-30、C-68、C-69。
本脚本的输出直接嵌入协议 §4.3，同时作为测试断言 T-1 / T-5 / T-20 的数据来源。

用法：python3 docs/_protocol_truth_table.py
"""

from itertools import product

NON_TERMINAL = [
    "QUEUED",
    "PREPARING",
    "AGENT_RUNNING",
    "PATCH_CAPTURED",
    "TESTING",
    "JUDGING",
    "ANALYZING",
]
TERMINAL = ["COMPLETED", "FAILED", "CANCELLED"]
LIFECYCLE = NON_TERMINAL + TERMINAL

INFRA = [
    "SUCCESS",
    "AGENT_TIMEOUT",
    "AGENT_RUNTIME_ERROR",
    "PATCH_APPLY_FAILED",
    "AGENT_AUTH_ERROR",
    "ENV_BUILD_FAILED",
    "WORKSPACE_ERROR",
    "SANDBOX_ERROR",
    "OOM_KILLED",
    "TEST_TIMEOUT",
    "TEST_DISCOVERY_ERROR",
    "HARNESS_ERROR",
    "CANCELLED",
]

AGENT = ["RESOLVED", "UNRESOLVED", "EMPTY_PATCH", "INVALID_PATCH", "NOT_ATTEMPTED", "NULL"]

# infra_outcome → 允许的 (lifecycle, agent_outcome, 区分条件) 列表
# 条件为 "" 表示无附加条件
LEGAL = {
    "SUCCESS": [
        ("COMPLETED", "RESOLVED", "F2P 全过且 P2P 全过"),
        ("COMPLETED", "UNRESOLVED", "补丁非空但没同时满足两个条件"),
        ("COMPLETED", "EMPTY_PATCH", "AI 正常退出且过滤后补丁为空"),
    ],
    "AGENT_TIMEOUT": [
        ("COMPLETED", "UNRESOLVED", "补丁可为空（C-08 例外）"),
    ],
    "AGENT_RUNTIME_ERROR": [
        ("COMPLETED", "UNRESOLVED", "每个 attempt 各自定性，与是否还会重试无关"),
    ],
    "PATCH_APPLY_FAILED": [
        ("COMPLETED", "INVALID_PATCH", ""),
    ],
    "AGENT_AUTH_ERROR": [
        ("FAILED", "NOT_ATTEMPTED", "agent_started_at IS NULL（容器启动前就鉴权失败）"),
        ("FAILED", "NULL", "agent_started_at IS NOT NULL（跑起来后才 401）"),
    ],
    "ENV_BUILD_FAILED": [
        ("FAILED", "NOT_ATTEMPTED", "只可能发生在 PREPARING，AI 必然未启动"),
    ],
    "WORKSPACE_ERROR": [
        ("FAILED", "NOT_ATTEMPTED", "只可能发生在 PREPARING，AI 必然未启动"),
    ],
    "SANDBOX_ERROR": [
        ("FAILED", "NOT_ATTEMPTED", "agent_started_at IS NULL（建 Agent 容器就失败）"),
        ("FAILED", "NULL", "agent_started_at IS NOT NULL（建测试容器时失败）"),
    ],
    "OOM_KILLED": [
        ("FAILED", "NULL", "只可能发生在 AGENT_RUNNING 或 TESTING，AI 必然已启动"),
    ],
    "TEST_TIMEOUT": [
        ("COMPLETED", "UNRESOLVED", "对照组正常，只有打了补丁才超时 → AI 的问题（C-20 第 4 步）"),
        ("FAILED", "NULL", "对照组也超时 → 环境问题（C-20 第 5 步）"),
    ],
    "TEST_DISCOVERY_ERROR": [
        ("FAILED", "NULL", "只可能发生在 JUDGING，AI 必然已启动"),
    ],
    "HARNESS_ERROR": [
        ("FAILED", "NOT_ATTEMPTED", "agent_started_at IS NULL"),
        ("FAILED", "NULL", "agent_started_at IS NOT NULL"),
    ],
    "CANCELLED": [
        ("CANCELLED", "NULL", ""),
    ],
}


def classify(life, infra, agent):
    """返回 (标记, 说明)。标记 ∈ {合法, 非法, 不可能}"""
    if life in NON_TERMINAL:
        if agent == "NULL":
            return "合法", "非终态，agent_outcome 必为 NULL（C-09）"
        return "非法", "非终态不允许有 agent_outcome（C-09）"
    for legal_life, legal_agent, cond in LEGAL.get(infra, []):
        if legal_life == life and legal_agent == agent:
            return "合法", cond
    # 终态但不在合法表里
    if any(legal_life == life for (legal_life, _, _) in LEGAL.get(infra, [])):
        return "非法", f"{infra} 在 {life} 下不允许取 {agent}"
    return "不可能", f"{infra} 不会以 {life} 结束"


def main():
    rows = [
        (life, infra, agent, *classify(life, infra, agent))
        for life, infra, agent in product(LIFECYCLE, INFRA, AGENT)
    ]
    legal = [r for r in rows if r[3] == "合法"]
    print(f"穷举组合总数：{len(rows)}")
    print(
        f"  合法 {len(legal)} · 非法 {sum(1 for r in rows if r[3] == '非法')} "
        f"· 不可能 {sum(1 for r in rows if r[3] == '不可能')}"
    )

    print("\n── 空洞检查（每个取值是否都能到达）──")
    ok = True
    checks = [
        ("lifecycle 终态", TERMINAL, 0),
        ("infra_outcome", INFRA, 1),
        ("agent_outcome", AGENT, 2),
    ]
    for name, pool, idx in checks:
        unreachable = [v for v in pool if not any(r[idx] == v for r in legal)]
        print(
            f"  {name}: {'✅ 全部可达' if not unreachable else '❌ 到不了 → ' + str(unreachable)}"
        )
        ok &= not unreachable

    print("\n── 重叠检查（同一 lifecycle+infra 是否有多个 agent_outcome 且缺少区分条件）──")
    for infra in INFRA:
        for life in TERMINAL:
            hits = [
                (legal_agent, cond)
                for (legal_life, legal_agent, cond) in LEGAL.get(infra, [])
                if legal_life == life
            ]
            if len(hits) > 1:
                if any(not c for _, c in hits):
                    print(f"  ❌ {life} + {infra}: 多个取值但有一个没写区分条件 → {hits}")
                    ok = False
                else:
                    names = [agent for agent, _ in hits]
                    print(f"  ⚠️  {life} + {infra}: {len(hits)} 个取值，靠条件区分 → {names}")
    print("  （标 ⚠️ 的是设计上就需要靠附加条件区分的，不是问题）")

    print("\n── 合法组合明细（可直接嵌入协议）──\n")
    print("| `lifecycle_status` | `infra_outcome` | `agent_outcome` | 区分条件 |")
    print("|:---|:---|:---|:---|")
    print("| 全部非终态 | 任意 | `NULL` | 非终态一律为空（C-09） |")
    for life in TERMINAL:
        for infra in INFRA:
            for legal_life, legal_agent, cond in LEGAL.get(infra, []):
                if legal_life == life:
                    print(f"| `{life}` | `{infra}` | `{legal_agent}` | {cond or '—'} |")

    print(f"\n结论：{'✅ 无空洞、无未区分的重叠' if ok else '❌ 存在问题，见上'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
