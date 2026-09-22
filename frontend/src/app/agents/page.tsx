"use client";

import { agentGroup, pilotHistoryConfigIds } from "@/lib/agents";
import { useAgentConfigs, useAllRuns, type AgentConfigSummary } from "@/lib/queries";

/**
 * 价格列：库里是 NUMERIC，JSON 里是字符串（固定 4 位小数）；null 表示还没录价。
 *
 * 统一显示两位小数（E7 走查 #19）。库里现在的单价都是 0.27 / 0.30 / 1.10 / 1.20
 * 这种量级，两位小数不会丢信息；真出现 0.003 这种更小的单价，两位小数会舍成
 * $0.00——到那时候再改成变长精度，不要现在猜。
 */
function price(value: string | null): string {
  if (value === null) return "—";
  return `$${Number(value).toFixed(2)}`;
}

function ConfigRow({
  config,
  pilotHistory,
}: {
  config: AgentConfigSummary;
  pilotHistory: boolean;
}) {
  return (
    <tr
      className={`border-b border-neutral-200 last:border-b-0 ${pilotHistory ? "text-neutral-400" : ""}`}
    >
      <td className="px-4 py-3">
        <div className="flex items-center gap-2">
          <span
            className={`text-sm font-medium ${pilotHistory ? "text-neutral-500" : "text-neutral-900"}`}
          >
            {config.agent_display_name}
          </span>
          {!config.enabled && (
            <span className="rounded bg-neutral-100 px-1.5 py-0.5 text-xs text-neutral-500">
              已停用
            </span>
          )}
          {pilotHistory && (
            <span
              title="同名的新配置已经跑过更晚的实验；这条不是当前用来打分的配置"
              className="rounded bg-neutral-100 px-1.5 py-0.5 text-xs text-neutral-500"
            >
              pilot 历史
            </span>
          )}
        </div>
        <p className="mt-0.5 text-xs text-neutral-500">{config.label}</p>
      </td>
      <td className="px-4 py-3 font-mono text-xs text-neutral-600">
        {config.agent_version}
      </td>
      <td className="px-4 py-3 font-mono text-xs text-neutral-600">
        {config.model_name}
      </td>
      <td className="px-4 py-3 text-right font-mono text-xs text-neutral-600">
        {price(config.price_input_per_mtok)}
      </td>
      <td className="px-4 py-3 text-right font-mono text-xs text-neutral-600">
        {price(config.price_output_per_mtok)}
      </td>
    </tr>
  );
}

function ConfigTable({
  configs,
  pilots,
}: {
  configs: AgentConfigSummary[];
  pilots: Set<number>;
}) {
  return (
    <div className="overflow-hidden rounded-md border border-neutral-200 bg-white">
      <table className="w-full">
        <thead>
          <tr className="border-b border-neutral-200 bg-neutral-50 text-left">
            <th className="px-4 py-2 text-xs font-medium text-neutral-600">Agent</th>
            <th className="px-4 py-2 text-xs font-medium text-neutral-600">版本</th>
            <th className="px-4 py-2 text-xs font-medium text-neutral-600">模型</th>
            <th className="px-4 py-2 text-right text-xs font-medium text-neutral-600">
              输入 / MTok
            </th>
            <th className="px-4 py-2 text-right text-xs font-medium text-neutral-600">
              输出 / MTok
            </th>
          </tr>
        </thead>
        <tbody>
          {configs.map((config) => (
            <ConfigRow key={config.id} config={config} pilotHistory={pilots.has(config.id)} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Agent 与配置。
 *
 * 一页列全：每个「适配器 × 模型 × 参数」的组合是一条配置，
 * 评测跑的是配置不是适配器 —— 同一个 Agent 换模型就是另一条配置。
 *
 * 分两组（E7 走查 #19）：参赛者 / 哨兵与诊断。哨兵（Oracle/Noop/Mock）不参赛，
 * 停用的诊断配置（比如 `aider@deepseek-chat+autotest`）也不参赛，混在参赛者
 * 里会让人以为库里有 5、6 个参赛 Agent。分组口径复用 `lib/dashboard.ts` 的
 * `isContestant`，和首页"几个参赛者"是同一个判断，不会两处漂移。
 *
 * 参赛者里 aider、Claude Code 各有两条启用配置（旧的 deepseek-chat、新的
 * deepseek-flash），配置本身分不出哪条是当前在用的——两条都启用、都跑过完整
 * 实验。按"最近一次实验谁更晚"标出旧的那条（`lib/agents.ts::pilotHistoryConfigIds`），
 * 灰显 + "pilot 历史" 标签，但不是遮住，改配置之前还能看见它长什么样。
 *
 * 设计稿里还有一列「可用性自检（probe）」，这里去掉了：
 * 数据库里没有它的落点（§14.5 第三条 —— probe 结果不进 P0 的表结构），
 * 前端凭空画一个永远空着的列，不如不画。真要加，是后端加列 + 迁移的事，
 * 超出 E7 的范围。
 *
 * 这里是 P0 唯一的写入口之外的管理页；目前只读。
 */
export default function AgentsPage() {
  const { data, error, isLoading } = useAgentConfigs();
  const { data: runs } = useAllRuns();

  const configs = data?.items ?? [];
  const contestants = configs.filter((config) => agentGroup(config) === "CONTESTANT");
  const sentinelsAndDiagnostics = configs.filter(
    (config) => agentGroup(config) === "SENTINEL_OR_DIAGNOSTIC",
  );
  const pilots = pilotHistoryConfigIds(configs, runs ?? []);

  return (
    <div className="mx-auto w-full max-w-4xl">
      <header className="border-b border-neutral-200 pb-6">
        <h1 className="text-2xl font-semibold tracking-tight">Agent 与配置</h1>
        <p className="mt-2 text-sm leading-relaxed text-neutral-600">
          参赛的 AI 及其配置。同一条配置在排行榜上是一个独立条目。
        </p>
      </header>

      <section className="mt-8">
        {isLoading && <p className="text-sm text-neutral-500">正在加载……</p>}

        {error && (
          <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3">
            <p className="text-sm text-red-700">取不到 Agent 配置。</p>
            <p className="mt-1 text-xs text-red-600">
              后端没起来的话，先跑 <code className="font-mono">make dev-api</code>。
            </p>
          </div>
        )}

        {data && configs.length === 0 && (
          <p className="text-sm text-neutral-500">
            还没有任何配置。跑 <code className="font-mono">make seed</code>{" "}
            灌一批种子数据。
          </p>
        )}

        {data && contestants.length > 0 && (
          <div className="mb-8">
            <h2 className="mb-2 text-sm font-semibold text-neutral-900">参赛者</h2>
            <ConfigTable configs={contestants} pilots={pilots} />
          </div>
        )}

        {data && sentinelsAndDiagnostics.length > 0 && (
          <div>
            <h2 className="mb-2 text-sm font-semibold text-neutral-900">哨兵与诊断</h2>
            <p className="mb-2 text-xs text-neutral-500">
              Oracle / Noop / Mock 三个哨兵用来验证判定引擎；诊断配置是一次性调试用的，都不参赛。
            </p>
            <ConfigTable configs={sentinelsAndDiagnostics} pilots={pilots} />
          </div>
        )}

        {data && configs.length > 0 && (
          <p className="mt-2 text-xs text-neutral-500">
            共 {data.total} 条配置（参赛者 {contestants.length} 条、哨兵与诊断{" "}
            {sentinelsAndDiagnostics.length} 条）。
          </p>
        )}
      </section>
    </div>
  );
}
