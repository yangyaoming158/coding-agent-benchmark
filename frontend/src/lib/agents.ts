import { isContestant, type AgentConfigSummary, type RunSummary } from "./dashboard";

/** `/agents` 页的两组：参赛者，和不参赛的哨兵/停用的诊断配置。 */
export type AgentGroupKey = "CONTESTANT" | "SENTINEL_OR_DIAGNOSTIC";

export function agentGroup(
  config: Pick<AgentConfigSummary, "agent_kind" | "enabled">,
): AgentGroupKey {
  return isContestant(config) ? "CONTESTANT" : "SENTINEL_OR_DIAGNOSTIC";
}

/**
 * 找出被同名新配置取代的旧配置（E7 走查 #19）。
 *
 * 库里 aider、Claude Code 各有两条启用中的参赛配置：旧的（deepseek-chat）和
 * 新的（deepseek-flash）。配置本身看不出哪条是"当前在用"的——两条都启用、
 * 都有过完整的实验。区分办法是看**最近一次实验是什么时候跑的**：同一个
 * Agent 显示名下，最近一次实验最晚的那条算当前配置，其余的标成"pilot 历史"
 * 灰显，即使它自己也跑过正式实验。
 *
 * 只有 2 条以上同名参赛配置时才会标——只有一条配置的 Agent（比如 MiniAgent）
 * 不会被灰显，"还没跑过实验"和"被取代了"是两回事。
 */
export function pilotHistoryConfigIds(
  configs: AgentConfigSummary[],
  runs: Pick<RunSummary, "agent_config_id" | "created_at">[],
): Set<number> {
  const lastRunAt = new Map<number, number>();
  for (const run of runs) {
    const at = Date.parse(run.created_at);
    const prev = lastRunAt.get(run.agent_config_id);
    if (prev === undefined || at > prev) lastRunAt.set(run.agent_config_id, at);
  }

  const byDisplayName = new Map<string, AgentConfigSummary[]>();
  for (const config of configs) {
    if (agentGroup(config) !== "CONTESTANT") continue;
    const group = byDisplayName.get(config.agent_display_name) ?? [];
    group.push(config);
    byDisplayName.set(config.agent_display_name, group);
  }

  const pilots = new Set<number>();
  for (const group of byDisplayName.values()) {
    if (group.length < 2) continue;
    let currentId = group[0].id;
    let currentAt = lastRunAt.get(currentId) ?? -Infinity;
    for (const config of group.slice(1)) {
      const at = lastRunAt.get(config.id) ?? -Infinity;
      if (at > currentAt) {
        currentAt = at;
        currentId = config.id;
      }
    }
    for (const config of group) {
      if (config.id !== currentId) pilots.add(config.id);
    }
  }
  return pilots;
}
