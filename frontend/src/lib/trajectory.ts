/**
 * 轨迹（`TRAJECTORY` 制品）的解析。§9.5：统一 JSONL，每行一个事件。
 *
 * 三类「确定能对上」的事件，各家适配器的产出：
 *
 *     {"ts": 1767…, "type": "tool_call",  "name": "edit_file", "args_digest": "…", "summary": "…"}
 *     {"ts": 1767…, "type": "llm_usage",  "input": 8123, "output": 412}
 *     {"ts": 1767…, "type": "message",    "role": "assistant", "text_excerpt": "…"}
 *
 * MiniAgent 原生输出三类；Claude Code 由 stream-json 转换；Aider 只产出
 * `llm_usage` 和 `tool_call`（§9.5 实测回填：它的自然语言里哪句是"思考"
 * 不去猜，猜出来的证据比没有证据更糟 —— 这条纪律对展示层同样适用）。
 *
 * 两条纪律：
 *
 * 1. **坏行不能让整份轨迹打不开。** 轨迹是诊断用的，解析失败就整页空白的工具
 *    在最需要它的时候正好用不了。坏行跳过去，计数交给界面提示。
 * 2. **不认识的事件原样留着。** 适配器以后新增事件类型时，老前端应该显示
 *    原始 JSON，而不是假装这行不存在。
 */

export type TrajectoryEvent =
  | { kind: "tool_call"; ts: number | null; name: string; digest: string | null; summary: string }
  | { kind: "llm_usage"; ts: number | null; input: number | null; output: number | null }
  | { kind: "message"; ts: number | null; role: string | null; text: string }
  | { kind: "unknown"; ts: number | null; type: string; raw: string };

export interface TrajectoryParseResult {
  events: TrajectoryEvent[];
  /** 解析不了的行数（不是 JSON、或者不是对象）。空行不计入。 */
  skipped: number;
}

/**
 * 轨迹时间戳统一成毫秒。
 *
 * 实际产出是毫秒（`miniagent_runtime.py` 写的是 `int(time.time() * 1000)`），
 * 但 §9.5 的文档示例只写了 `1767...`，看不出单位。这里的下界判断是防呆：
 * 1e12 毫秒是 2001 年，1e12 秒是公元 33658 年 —— 小于 1e12 的数不可能是毫秒。
 */
export function eventMillis(ts: number | null): number | null {
  if (ts === null) return null;
  return ts < 1e12 ? ts * 1000 : ts;
}

export function parseTrajectory(text: string): TrajectoryParseResult {
  const events: TrajectoryEvent[] = [];
  let skipped = 0;

  for (const rawLine of text.split("\n")) {
    const line = rawLine.trim();
    if (line === "") continue;

    let value: unknown;
    try {
      value = JSON.parse(line);
    } catch {
      skipped++;
      continue;
    }
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
      skipped++;
      continue;
    }

    const obj = value as Record<string, unknown>;
    const ts = typeof obj.ts === "number" ? obj.ts : null;
    const type = typeof obj.type === "string" ? obj.type : "";

    switch (type) {
      case "tool_call":
        events.push({
          kind: "tool_call",
          ts,
          name: typeof obj.name === "string" ? obj.name : "(未命名工具)",
          digest: typeof obj.args_digest === "string" ? obj.args_digest : null,
          summary: typeof obj.summary === "string" ? obj.summary : "",
        });
        break;
      case "llm_usage":
        events.push({
          kind: "llm_usage",
          ts,
          input: typeof obj.input === "number" ? obj.input : null,
          output: typeof obj.output === "number" ? obj.output : null,
        });
        break;
      case "message":
        events.push({
          kind: "message",
          ts,
          role: typeof obj.role === "string" ? obj.role : null,
          text: typeof obj.text_excerpt === "string" ? obj.text_excerpt : "",
        });
        break;
      default:
        events.push({ kind: "unknown", ts, type, raw: line });
    }
  }

  return { events, skipped };
}

/** 各类事件条数，给折叠摘要用。 */
export function countEvents(events: TrajectoryEvent[]): Record<TrajectoryEvent["kind"], number> {
  const counts = { tool_call: 0, llm_usage: 0, message: 0, unknown: 0 };
  for (const e of events) counts[e.kind]++;
  return counts;
}
