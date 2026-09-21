/**
 * 展示层的翻译：枚举 → 中文标签，数字 → 带单位的字符串。
 *
 * 为什么不在后端翻好：E7-T0 的 AC-10 定了枚举**原样透出**。
 * 协议 C-04/C-05/C-06 的 `lifecycle_status` / `infra_outcome` / `agent_outcome`
 * 是三个互相独立的字段，前端要按原值分面、着色、统计 —— 后端一旦翻成中文，
 * 这几个字段就再也拼不回去了。所以翻译只能发生在最外一层，也就是这里。
 */

import type { components } from "./api-types";

type Schemas = components["schemas"];

export type RunStatus = Schemas["EvaluationRunStatus"];
export type LifecycleStatus = Schemas["LifecycleStatus"];
export type InfraOutcome = Schemas["InfraOutcome"];
export type AgentOutcome = Schemas["AgentOutcome"];
export type TestStatus = Schemas["TestStatus"];
export type TestRole = Schemas["TestRole"];
export type CostSource = Schemas["CostSource"];

/** 界面上的颜色档。components 拿去映射成具体的 class，这里不带 Tailwind。 */
export type Tone = "neutral" | "active" | "ok" | "warn" | "bad";

// ── 实验状态 ────────────────────────────────────────────────

const RUN_STATUS_TEXT: Record<RunStatus, { label: string; tone: Tone }> = {
  DRAFT: { label: "草稿", tone: "neutral" },
  QUEUED: { label: "排队中", tone: "warn" },
  RUNNING: { label: "运行中", tone: "active" },
  COMPLETED: { label: "已完成", tone: "ok" },
  PARTIAL: { label: "部分完成", tone: "warn" },
  FAILED: { label: "失败", tone: "bad" },
  CANCELLED: { label: "已取消", tone: "neutral" },
};

export function runStatusLabel(status: RunStatus): string {
  return RUN_STATUS_TEXT[status].label;
}

export function runStatusTone(status: RunStatus): Tone {
  return RUN_STATUS_TEXT[status].tone;
}

/** 还在动的实验。只有这几种才值得开轮询（§16.1 的 3s 是给它们定的）。 */
const LIVE_RUN_STATUSES: RunStatus[] = ["DRAFT", "QUEUED", "RUNNING"];

export function isLiveRun(status: RunStatus): boolean {
  return LIVE_RUN_STATUSES.includes(status);
}

// ── 逐题生命周期 ────────────────────────────────────────────

const LIFECYCLE_TEXT: Record<LifecycleStatus, string> = {
  QUEUED: "排队中",
  PREPARING: "准备中",
  AGENT_RUNNING: "Agent 执行中",
  PATCH_CAPTURED: "已捕获补丁",
  TESTING: "测试中",
  JUDGING: "判定中",
  ANALYZING: "归因中",
  COMPLETED: "已完成",
  FAILED: "平台故障",
  CANCELLED: "已取消",
};

export function lifecycleLabel(status: LifecycleStatus): string {
  return LIFECYCLE_TEXT[status];
}

/**
 * 这一次执行还在不在动。终态只有三种（C-24），其余都还在流程里 ——
 * 和 `cellVerdict` 用的是同一个 `TERMINAL`，两处判定不能各写一份。
 */
export function isLiveTaskRun(status: LifecycleStatus): boolean {
  return !TERMINAL.includes(status);
}

// ── 平台故障的细分 ──────────────────────────────────────────

const INFRA_TEXT: Record<InfraOutcome, string> = {
  SUCCESS: "正常",
  ENV_BUILD_FAILED: "环境构建失败",
  WORKSPACE_ERROR: "工作区错误",
  AGENT_TIMEOUT: "Agent 超时",
  AGENT_RUNTIME_ERROR: "Agent 运行时错误",
  AGENT_AUTH_ERROR: "Agent 认证失败",
  SANDBOX_ERROR: "沙箱错误",
  OOM_KILLED: "内存超限被杀",
  TEST_TIMEOUT: "测试超时",
  TEST_DISCOVERY_ERROR: "用例发现失败",
  PATCH_APPLY_FAILED: "补丁应用失败",
  HARNESS_ERROR: "平台自身错误",
  CANCELLED: "已取消",
};

export function infraLabel(outcome: InfraOutcome): string {
  return INFRA_TEXT[outcome];
}

// ── 成本的来源 ──────────────────────────────────────────────

/**
 * 协议纪律 3：金额是从哪来的必须区分显示。
 *
 * `unavailable` 时金额那一格是空的，**不是 0** —— 显示成 $0 会让人以为
 * 这个 Agent 不花钱。§18.6 第七节手算过一次：claude-code 全报 unavailable、
 * 总额是 0，但实际每题 $0.042，比 aider 贵 2.4 倍。
 *
 * 注意这几个值是小写，和 lifecycle_status 那三个大写枚举不一样 ——
 * 以后端 OpenAPI 为准，别照印象写。
 */
const COST_SOURCE_TEXT: Record<CostSource, string> = {
  reported: "Agent 自报",
  estimated: "平台估算",
  unavailable: "拿不到",
};

export function costSourceLabel(source: CostSource): string {
  return COST_SOURCE_TEXT[source];
}

// ── 逐条用例的状态 ──────────────────────────────────────────

/**
 * 一条用例的判定结果（协议 C-10）。
 *
 * `MISSING` 单独标成 warn 而不是 neutral：它表示题目里列了这条用例、
 * 测试报告里却找不到 —— C-11 把它记成缺失，C-12 明令**禁止**把它当成通过。
 * 显示成一个中性的「跳过」会让人错过它，而这正是归一化 bug 的典型症状。
 */
const TEST_STATUS_TEXT: Record<TestStatus, { label: string; tone: Tone }> = {
  PASSED: { label: "通过", tone: "ok" },
  FAILED: { label: "失败", tone: "bad" },
  ERROR: { label: "错误", tone: "bad" },
  SKIPPED: { label: "跳过", tone: "neutral" },
  XFAIL: { label: "预期失败", tone: "neutral" },
  XPASS: { label: "意外通过", tone: "warn" },
  MISSING: { label: "缺失", tone: "warn" },
};

export function testStatusText(status: TestStatus): { label: string; tone: Tone } {
  return TEST_STATUS_TEXT[status];
}

// ── 网格格子的判定 ──────────────────────────────────────────

/**
 * 网格里的分组。**这不是给后端枚举做同义词替换**，是按"谁的问题"归类 ——
 * 看一次实验时真正要回答的是三件事：几道解决了、几道是 AI 没搞定、
 * 几道根本不是 AI 的锅（那部分要从解决率的分母里剔掉）。
 */
export type CellBucket = "active" | "resolved" | "unresolved" | "infra" | "cancelled";

export const BUCKET_ORDER: CellBucket[] = [
  "active",
  "resolved",
  "unresolved",
  "infra",
  "cancelled",
];

export const BUCKET_LABEL: Record<CellBucket, string> = {
  active: "进行中",
  resolved: "已解决",
  unresolved: "未解决",
  infra: "平台故障",
  cancelled: "已取消",
};

export interface CellVerdict {
  bucket: CellBucket;
  tone: Tone;
  /** 格子悬停时显示的判定文字 */
  label: string;
}

/** 后端没有直接给"谁的问题"，这里从生命周期状态推。 */
interface VerdictInput {
  lifecycle_status: LifecycleStatus;
  infra_outcome: InfraOutcome | null;
  agent_outcome: AgentOutcome | null;
  error_code: string | null;
}

const TERMINAL: LifecycleStatus[] = ["COMPLETED", "FAILED", "CANCELLED"];

/**
 * 一道题最后算怎么回事。
 *
 * **判定权威是 `lifecycle_status`，不是 `infra_outcome`。** 这个反直觉，
 * 但是协议定的（`app/judge/decision.py` 的 `_LIFECYCLE_BY_OWNER`）：
 *
 * - 故障归 AI → `COMPLETED`（"拿到了结论"，哪怕结论是没修好）
 * - 故障归平台/外部 → `FAILED`（"没拿到结论"）
 * - 人取消 → `CANCELLED`
 *
 * 所以 `FAILED` 才等价于"不是 AI 的锅"。反过来，`infra_outcome` 不是 SUCCESS
 * 却仍是 `COMPLETED` 的情况是存在的、而且是**算在 AI 头上**的 ——
 * 典型是 AI 自己跑超时（C-20 第 4 步：只有打了 AI 补丁才超时，那是 AI 的问题）。
 * 拿 `infra_outcome` 去着色会把这道题误判成平台故障，把 AI 的锅甩给环境。
 */
export function cellVerdict(run: VerdictInput): CellVerdict {
  if (!TERMINAL.includes(run.lifecycle_status)) {
    return {
      bucket: "active",
      tone: "active",
      label: lifecycleLabel(run.lifecycle_status),
    };
  }

  if (run.lifecycle_status === "CANCELLED") {
    return { bucket: "cancelled", tone: "neutral", label: "已取消" };
  }

  if (run.lifecycle_status === "FAILED") {
    const detail = run.infra_outcome ? infraLabel(run.infra_outcome) : "未给出细节";
    return { bucket: "infra", tone: "warn", label: `平台故障 · ${detail}` };
  }

  // 到这里 lifecycle_status 一定是 COMPLETED：故障归 AI，看它修没修好
  switch (run.agent_outcome) {
    case "RESOLVED":
      return { bucket: "resolved", tone: "ok", label: "已解决" };
    case "UNRESOLVED":
      return { bucket: "unresolved", tone: "bad", label: "未解决" };
    case "EMPTY_PATCH":
      // C-08a：标准化**之后**补丁为空才算。不等于"AI 什么都没做" ——
      // 它可能改了一堆受保护路径被 C-41 全丢了，两种情况要靠 C-08b 的两个标志区分
      return { bucket: "unresolved", tone: "bad", label: "空补丁" };
    case "INVALID_PATCH":
      return { bucket: "unresolved", tone: "bad", label: "补丁无效" };
    case "NOT_ATTEMPTED":
      return { bucket: "unresolved", tone: "neutral", label: "未尝试" };
    default:
      return { bucket: "unresolved", tone: "neutral", label: "已结束（无判定）" };
  }
}

// ── 数字格式化 ──────────────────────────────────────────────

/** 解决率。后端给的是 0–1 的比例字符串（4 位小数），不是百分数。 */
export function formatRate(rate: string | null): string {
  if (rate === null) return "—";
  const n = Number(rate);
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : rate;
}

/**
 * 金额。
 *
 * 一元以下保留 4 位小数：单题成本能小到 $0.0175（aider 在 §18.6 里的实测值），
 * 两位小数会把它显示成 $0.02 —— 差了 14%。成本数字是要拿来横向比的，
 * 这个量级的误差不能算"显示细节"。一元以上两位小数就够，也更好读。
 */
export function formatCost(usd: string | null): string {
  if (usd === null) return "—";
  const n = Number(usd);
  if (!Number.isFinite(n)) return usd;
  if (n === 0) return "$0";
  if (n < 1) return `$${n.toFixed(4).replace(/0+$/, "").replace(/\.$/, "")}`;
  return `$${n.toFixed(2)}`;
}

export function formatDuration(ms: number | null): string {
  if (ms === null) return "—";
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`;
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

/** 制品大小（原始内容的字节数，不是压缩后占的磁盘 —— 见 §17.4）。 */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * 单条用例的耗时。多数在毫秒级，而 `formatDuration` 的最小刻度是秒 ——
 * 120 ms 会被它四舍五入成"0 秒"，看证据的人会以为这条用例没跑。
 */
export function formatMillis(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${ms} ms`;
  return formatDuration(ms);
}

export function formatTokens(tokens: number | null): string {
  if (tokens === null) return "—";
  if (tokens < 1000) return String(tokens);
  if (tokens < 1_000_000) return `${(tokens / 1000).toFixed(1)}k`;
  return `${(tokens / 1_000_000).toFixed(2)}M`;
}

/**
 * 时间戳。
 *
 * 只在客户端调用：这几个页面的时间是跟着请求回来的数据渲染的，
 * 首屏（loading 态）里没有它，所以不存在服务端/浏览器时区不一致导致的
 * hydration 报错。真要在 SSR 阶段渲染时间，得先定一个时区。
 */
export function formatTime(iso: string | null): string {
  if (iso === null) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("zh-CN", { hour12: false });
}
