"use client";

import { useQuery } from "@tanstack/react-query";
import { apiGet, type Health } from "@/lib/api";

/** 一行状态。`tone` 决定左边那个小圆点的颜色。 */
function Row({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string;
  tone?: "ok" | "bad" | "neutral";
}) {
  const dot =
    tone === "ok"
      ? "bg-emerald-500"
      : tone === "bad"
        ? "bg-red-500"
        : "bg-neutral-300";
  return (
    <div className="flex items-center justify-between border-b border-neutral-200 py-2.5 last:border-b-0">
      <span className="text-sm text-neutral-600">{label}</span>
      <span className="flex items-center gap-2 font-mono text-sm text-neutral-900">
        <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
        {value}
      </span>
    </div>
  );
}

/**
 * 平台自检。
 *
 * 轮询而不是 WebSocket（§16.1 的决定）：实现成本接近 0，没有连接管理，
 * 而这里要回答的问题只是"后端还活着吗"，不需要毫秒级。
 */
export function PlatformStatus() {
  const { data, error, isLoading } = useQuery({
    queryKey: ["health"],
    queryFn: () => apiGet<Health>("/api/health"),
    refetchInterval: 10_000,
  });

  return (
    <section className="mt-8">
      <h2 className="text-sm font-semibold text-neutral-900">平台自检</h2>
      <div className="mt-3 rounded-md border border-neutral-200 bg-white px-4 py-1">
        {isLoading && (
          <p className="py-3 text-sm text-neutral-500">正在连接后端……</p>
        )}

        {error && (
          <div className="py-3">
            <p className="text-sm text-red-600">连不上后端。</p>
            <p className="mt-1 text-xs text-neutral-500">
              先跑 <code className="font-mono">make dev</code>，或者单独起后端：
              <code className="font-mono">
                cd backend &amp;&amp; uv run uvicorn app.main:app --reload
              </code>
            </p>
          </div>
        )}

        {data && (
          <>
            <Row
              label="服务状态"
              value={data.status}
              tone={data.status === "ok" ? "ok" : "bad"}
            />
            <Row
              label="数据库"
              value={data.database}
              tone={data.database === "ok" ? "ok" : "bad"}
            />
            <Row
              label="迁移版本"
              value={data.migration_revision ?? "未初始化"}
              tone={data.migration_revision ? "ok" : "bad"}
            />
            <Row label="评测协议" value={data.protocol_version} tone="ok" />
          </>
        )}
      </div>
      {data?.status === "degraded" && (
        <p className="mt-2 text-xs text-neutral-500">
          degraded 表示后端起来了但数据库没准备好。跑{" "}
          <code className="font-mono">make db-up &amp;&amp; make migrate</code>。
        </p>
      )}
    </section>
  );
}
