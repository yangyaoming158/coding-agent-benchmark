"use client";

import { useAgentConfigs, type AgentConfigSummary } from "@/lib/queries";

/**
 * 价格列：库里是 NUMERIC，JSON 里是字符串（固定 4 位小数）；null 表示还没录价。
 *
 * 走 Number → String 而不是 toFixed：后者会把 0.003 这种小额单价舍成 $0，
 * 而"单价是 0"和"单价很小"在成本对比里是两回事。String(Number(x)) 不补零也不舍入。
 */
function price(value: string | null): string {
  if (value === null) return "—";
  return `$${String(Number(value))}`;
}

function ConfigRow({ config }: { config: AgentConfigSummary }) {
  return (
    <tr className="border-b border-neutral-200 last:border-b-0">
      <td className="px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-neutral-900">
            {config.agent_display_name}
          </span>
          {!config.enabled && (
            <span className="rounded bg-neutral-100 px-1.5 py-0.5 text-xs text-neutral-500">
              已停用
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

/**
 * Agent 与配置。
 *
 * 一页列全：每个「适配器 × 模型 × 参数」的组合是一条配置，
 * 评测跑的是配置不是适配器 —— 同一个 Agent 换模型就是另一条配置。
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

        {data && data.items.length === 0 && (
          <p className="text-sm text-neutral-500">
            还没有任何配置。跑 <code className="font-mono">make seed</code>{" "}
            灌一批种子数据。
          </p>
        )}

        {data && data.items.length > 0 && (
          <div className="overflow-hidden rounded-md border border-neutral-200 bg-white">
            <table className="w-full">
              <thead>
                <tr className="border-b border-neutral-200 bg-neutral-50 text-left">
                  <th className="px-4 py-2 text-xs font-medium text-neutral-600">
                    Agent
                  </th>
                  <th className="px-4 py-2 text-xs font-medium text-neutral-600">
                    版本
                  </th>
                  <th className="px-4 py-2 text-xs font-medium text-neutral-600">
                    模型
                  </th>
                  <th className="px-4 py-2 text-right text-xs font-medium text-neutral-600">
                    输入 / MTok
                  </th>
                  <th className="px-4 py-2 text-right text-xs font-medium text-neutral-600">
                    输出 / MTok
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((config) => (
                  <ConfigRow key={config.id} config={config} />
                ))}
              </tbody>
            </table>
          </div>
        )}

        {data && data.items.length > 0 && (
          <p className="mt-2 text-xs text-neutral-500">
            共 {data.total} 条配置。
          </p>
        )}
      </section>
    </div>
  );
}
