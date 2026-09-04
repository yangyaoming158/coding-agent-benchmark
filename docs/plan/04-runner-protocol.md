# 9 Agent Runner Protocol（项目核心抽象）

## 9.1 学校"stdin 任务 → stdout 补丁"协议的现实性分析

学校文档写的是：**统一 Runner 协议（stdin任务 → stdout补丁）**。
但现实是：Claude Code、Codex CLI、Aider、Qwen Code 这些工具，**没有一个**是"从标准输入读任务、往标准输出打印补丁"这种用法。它们都是**直接把工作目录里的文件改掉**，标准输出打印的是它干活过程的自然语言描述。

| 方案 | 描述 | 可行性 |
|:---|:---|:---|
| **Strict Patch Runner** | 要求 Agent 自己在 stdout 打印 unified diff | 对真实 CLI **不可行**（需要给每个 CLI 写 prompt 让它"最后打印 diff"，极不稳定：会截断、会带 markdown 围栏、会写错行号） |
| **Workspace Mutation Runner** | Agent 自由改写 `/workspace`，**由 harness 执行 `git diff` 生成补丁** | **可行且稳健**：补丁由 git 生成，行号/上下文天然正确，必然可应用 |

### 怎么既满足学校要求又能真的跑起来

**保留学校协议的字面形式，改变协议的边界位置。**

协议主体不是"Agent 进程"，而是 **Adapter 进程**：

```
harness ──stdin(JSON: AgentTaskInput)──▶ [Adapter 进程] ──stdout(JSON: AgentRunResult)──▶ harness
                                              │
                                              ├─ 内部实现 A：调用真实 CLI 改写 /workspace，然后 git diff → patch 字段
                                              └─ 内部实现 B：Agent 自己产出 diff（strict 模式）→ patch 字段
```

- 对平台而言：**仍然是 stdin 任务 → stdout（含）补丁**，完全符合学校描述；
- 对真实 Agent 而言：不强迫它做做不到的事；
- 两种模式最终都归一化为 **`NormalizedPatch`**。

> 这一条写进 ADR-007。答辩时的表述："我们实现了学校要求的 stdin/stdout 协议，但把协议边界放在适配器上，从而兼容 workspace-mutation 型 Agent —— 这是让协议在真实 Agent 上成立的必要设计。"

## 9.2 协议数据结构

### 输入 `AgentTaskInput`（stdin，一行 JSON）
```jsonc
{
  "protocol_version": "1.0",
  "task_id": "...",
  "workspace_path": "/workspace",
  "issue": { "title": "...", "body": "...", "language": "zh" },
  "repo": { "name": "nonebot/nonebot2", "base_commit": "3f2a1c9e" },
  "hints": null,
  "constraints": {
    "deadline_unix_ms": 1767225600000,
    "max_tokens_budget": 600000,
    "protected_paths": ["tests/**", "conftest.py", "..."],   // 只含通用规则，改了也会被丢弃
                                                             // 这是 agent_visible_protected_paths
    "allow_network": true,
    "allow_run_tests": true                                   // 是否允许 Agent 自己跑测试
  },
  "model": { "name": "claude-sonnet-4-6", "temperature": 0.0 },
  "extra": {}                                                  // 适配器私有配置
}
```

> `test_command` **不下发**。若 `allow_run_tests=true`，Agent 可自行探索如何跑测试（这本身是 Coding Agent 的能力之一）；F2P 的具体用例 ID **绝不下发**（否则等于告诉它答案在哪）。
>
> **`protected_paths` 字段只放通用规则，绝不能放该题的 `test_patch_paths`。** 平台内部执行过滤时用的是另一份更完整的清单（含 `test_patch_paths`），两者不能混用。
>
> 为什么：如果把该题测试补丁实际改动的路径告诉 AI，等于直接指出"官方是改这几个文件来验证的"，是一种定位提示。虽然没有直接给出 F2P 用例 ID，但泄露程度是同一类的。详见协议 C-75、C-76。

### 输出 `AgentRunResult`（stdout，最后一行 JSON；其余行视为日志）
```jsonc
{
  "protocol_version": "1.0",
  "agent_name": "aider",
  "agent_version": "0.86.1",
  "model": "deepseek-chat",
  "started_at": "...", "finished_at": "...", "duration_ms": 384512,
  "exit_code": 0,
  "patch": "diff --git a/... ",          // unified diff；空字符串表示未改动
  "patch_source": "git_diff",            // git_diff | agent_stdout
  "token_usage": { "input": 213004, "output": 18422, "cache_read": 190000, "total": 231426 },
  "cost_usd": 0.0421,
  "cost_source": "reported",             // reported | estimated | unavailable
  "turns": 27,
  "trajectory_uri": "file:///artifacts/trajectory.jsonl",   // 适配器写文件，harness 收集
  "error": null,                          // {code, message} 结构
  "raw_stdout_bytes": 918234, "raw_stderr_bytes": 12044
}
```

**协议纪律**：
1. stdout 的**最后一行**必须是合法 JSON，前面允许任意日志（真实 CLI 一定会刷屏）；为稳健起见同时支持 `--result-file` 兜底（适配器把结果写文件，harness 优先读文件）；
2. 适配器**不得**自行判定是否解决，`AgentRunResult` 里没有 `resolved` 字段——判定权只属于 Judge；
3. `cost_source=unavailable` 时（订阅制 CLI 常见），平台按 `token_usage × 配置单价` 估算并标记 `estimated`，报告中必须区分显示。

## 9.3 Adapter 架构

```python
class AgentRunner(Protocol):
    name: str

    def probe(self) -> ProbeResult: ...  # 检查 CLI 存在、鉴权可用、版本号
    def run(self, task: AgentTaskInput, ws: Workspace, cfg: AgentConfig) -> AgentRunResult: ...


# 需要自带镜像层的适配器才实现这一个（Aider 要 pip 装、Claude Code 要 npm 装）。
# Mock / Oracle / Noop 不需要镜像，拆开是为了不让它们背一个只会 raise 的死方法 ——
# 那种方法契约测试永远测不到。
class ImageBuildingRunner(AgentRunner, Protocol):
    def build_image(self, environment_id: str, base_image: str) -> str: ...
```

实现类：`MockRunner`、`OracleRunner`(直接返回 gold patch，用于验证 harness 无假阴性)、`NoopRunner`(返回空补丁，解决率下界)、`AiderRunner`、`ClaudeCodeRunner`、`QwenCodeRunner`、`MiniAgentRunner`(自研)、`CodexRunner`(P2)。

**适配器契约测试套件**（每个适配器必须通过，用最便宜的模型跑）：
1. probe 成功；2. 在 golden task 上产出非空 patch；3. 触发 deadline 时优雅返回（不留孤儿进程）；4. 改了 protected_path 时该改动**留在原始补丁里**（剔除由平台做，见协议 C-08b）；5. token/cost 字段格式正确或明确 unavailable；6. stdout 含大量噪声时结果仍能解析。

> **2026-09-05 按实现回填**（E3-T1 / E3-T2）：本节的类名、`AgentRunner` 的形状、
> 契约第 4 条的方向，已对齐 `backend/app/runner/protocol.py` 和
> `backend/app/runner/adapters/`。第 4 条那句原来只写"被剔除"、没说谁剔除，照字面写
> 新适配器会让它自己过滤，而那正是这一条要抓的 bug —— 过滤掉之后
> `protected_path_edit_attempted` 就没有证据了。
> **§9.2 的报文格式（stdin 一行 JSON 任务 / stdout 最后一行 JSON 结果 / 结果里带
> unified diff）没有任何改动。**

## 9.4 真实 Agent 选型与顺序（关键决策）

评分维度（1–5 分，5 最好）：

| Agent | CLI 自动化 | 非交互 | 鉴权 | Patch 获取 | Token/Cost | Docker 兼容 | 稳定性 | 成本 | **4 周风险** |
|:---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--|
| **Aider** | 5 | 5 (`--yes-always --message`) | 5（纯 API Key，任意 OpenAI 兼容端点） | 5（workspace + 自带 git） | 5（自带 token/cost 统计输出） | 5（pip 安装） | 4 | 5（可接国产便宜模型） | **低** |
| **Claude Code** | 4 | 5 (`-p/--print`, `--output-format stream-json`, `--max-turns`) | 3（API Key 可脚本化；订阅态 OAuth 需预置凭据） | 5（workspace） | 4（stream-json 含 usage） | 4（npm 安装，需 Node） | 4 | 3（贵，或受订阅并发限制） | **中** |
| **Qwen Code**（国产） | 4 | 4 (`-p` 非交互) | 4（OpenAI 兼容 Key / DashScope） | 5（workspace） | 3 | 4（npm） | 3 | 5 | **中** |
| **自研 MiniAgent** | 5 | 5 | 5 | 5 | 5（自己算） | 5 | 5（自己修） | 5 | **低** |
| **Codex CLI** | 3 | 4 | 2（登录流程最麻烦，容器内尤甚） | 5 | 3 | 3 | 3 | 3 | **高** |
| OpenHands | 3 | 3 | 4 | 4 | 3 | 2（自身要 Docker，DinD 复杂） | 3 | 4 | **高** |

**推荐接入顺序：**

- **第 1 个真实 Agent：Aider**（Week 2 Day 1–2）
  理由：pip 一行装好；`aider --yes-always --no-auto-commits --no-check-update --no-stream --message "<issue>" <files?>` 完全非交互；纯 API Key 鉴权，无浏览器流程；可指向**任意 OpenAI 兼容端点**（意味着一个适配器 × N 个底座模型 = 多个"参赛者"，这是应对 Agent 数量风险的最强后手）；自带 token/cost 输出。**它是把"Agent 接入"这条风险线从高降到低的关键选择。**

- **第 2 个真实 Agent：Claude Code**（Week 2 Day 3–5）
  理由：学校点名要求；headless 模式成熟（`claude -p --output-format stream-json --max-turns N --permission-mode bypassPermissions`，容器内无人值守可行）；是"旗舰对照组"，报告说服力最强。风险点在鉴权与并发额度，因此排第 2 而非第 1。

- **第 3 个真实 Agent：自研 MiniAgent**（Week 3 Day 1–2）
  理由：满足学校"学生自研 Agent"的明确要求；**零外部风险**；实现量可控（ReAct 循环 + 4 个工具：`read_file / list_dir / grep / apply_edit`，可选 `run_tests`）；接国产便宜模型；答辩时能完整讲清"Agent 内部到底怎么工作"——**这是本项目最好的教学价值点**。

- **第 4 个：国产 CLI（Qwen Code 等）**（Week 3，P1）
  满足"国产 Coding Agent"要求。若受阻，用 MiniAgent + 国产模型顶替，并在报告中说明差异。

- **Codex CLI：P2**，仅当上述全部就绪且有余力再做。

**成本-能力矩阵（报告里的一张好图）**：把 5 个参赛者放在 `解决率 × 单题成本` 平面上，这比单一排行榜更有洞察力，也更贴合"评测平台"的定位。

## 9.5 轨迹（Trajectory）采集
统一为 JSONL，每行一个事件：
```jsonc
{"ts": 1767..., "type": "tool_call", "name": "edit_file", "args_digest": "sha256:..", "summary": "..."}
{"ts": 1767..., "type": "llm_usage", "input": 8123, "output": 412}
{"ts": 1767..., "type": "message", "role": "assistant", "text_excerpt": "..."}
```
- Claude Code：由 `--output-format stream-json` 直接转换；
- Aider：解析其输出 + `.aider.chat.history.md`；
- MiniAgent：原生输出；
- 无法采集时至少留全量 stdout（`raw_stdout` 制品）。
轨迹用于：① 失败归因的证据；② 人工抽检页展示；③ 报告"含每题轨迹"要求（FR-17）。

## 9.6 超时与清理
- 阶段级硬超时由 harness 用 `docker stop --time=10` + `docker kill` 双保险，不依赖 Agent 自觉；
- 每次运行的容器带 `--label bench.run_id=...`，运行结束后统一 `docker rm -f`；
- Worker 启动时执行**孤儿容器回收**（按 label 清理上次崩溃遗留），这是长跑实验稳定性的必要设施。
