# E3-T6 MiniAgent：实现与本地验收

2026-09-19，分支 `feat/E3-T6-miniagent`，基线 `c100a1c`。
实现和三条 AC 已通过；本记录形成时尚未提交，任务交付还需 PR review 和合并。

## AC 对账

| AC | 可复核证据 |
|:--|:--|
| Golden 至少解决 1 题 | 实验 **#145**，task run **#1407**，`bench-golden__auth-2`：`COMPLETED / SUCCESS / RESOLVED`，1/1，平台故障 0，重试 0，5.3 秒；平台独立测试容器 **7 passed** |
| 原生结构化 JSONL | `runs/145/tasks/314/attempt-1/trajectory.jsonl.gz`：4 条 `llm_usage`，5 条 `tool_call`，包含全部四种工具，以及 `message / stop / cost_estimate`；每条独立 JSON，记录时间、参数和工具返回 |
| 单题成本可核算 | 输入 **6082**（其中缓存命中 **4096**），输出 **471**，合计 **6553**；美元估算 **0.000592788**，数据库六位小数为 **0.000593**，`cost_source=estimated`；人民币估算 **0.00395192 元** |

工作区未提交，实验 `dirty=true`，仅开发验收，不能进排行榜。Golden 使用已有四道 VALID 题组成的 `golden@v1` **草稿**，没有冒充已发布数据集；本次没有改任何已发布版本。

## 实现范围

- `backend/app/runner/miniagent_runtime.py`：标准库 HTTP 调模型，执行 `read_file / list_dir / grep / apply_edit`，将工具结果交回下一轮。没有可选的 `run_tests`，由平台判定容器运行官方测试。
- `backend/app/runner/adapters/miniagent.py`：共享 `build_task_prompt()`；现有沙箱限制、原始 diff 捕获、`shared_failure()` 错误映射；累计实际 usage，缓存输入不重复计数。中断请求可能已收费但缺 usage，此时成本标 `unavailable`，保留已知用量。
- 文件工具禁止越界路径、符号链接和 `.git`，限制文件大小、扫描数和返回长度。适配器保留受保护文件的原始改动证据，由现有平台过滤、还原，不改判定语义。
- `backend/cli/miniagent.py`：stdin 读协议任务，stdout 最后一行返回结果；`--params-file` 接收 JSON 配置，`--artifact-dir` 保存轨迹，`--result-file` 可另存结果。
- Worker 从 `agent_configs.params` 构造 MiniAgent；`cli.seed` 增加自研 Agent 种子。本次生产库只新增 MiniAgent 两行，没有重跑全部种子覆盖其他 Agent 配置。
- 复用已有 `bench-base:py311`，将单个 runtime 文件只读挂入容器；没有建镜像、下载模型、增加依赖或修改 API / frontend / 冻结件。

## 模型与预算

免费 `/models` 查询实际返回 `deepseek-flash`、`deepseek-v4-pro`，不再列出原来的 `deepseek-chat`。
因此这次使用现有 DeepSeek 账号的 **`deepseek/deepseek-flash`**，关闭 thinking，温度按任务输入；没有把旧模型实验与本次当作同模型比较。

核价来源：[官方人民币价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)、[官方美元价格](https://api-docs.deepseek.com/quick_start/pricing/)，2026-09-19（周六，非高峰）。

| 每百万 token | 人民币 | 美元 |
|:--|--:|--:|
| 未缓存输入 | 1 | 0.15 |
| 缓存输入 | 0.02 | 0.003 |
| 输出 | 4 | 0.6 |

单题公式：`((6082 - 4096) × 1 + 4096 × 0.02 + 471 × 4) / 1000000 = 0.00395192 元`。
四次真实运行（Golden 一次 + 契约三次）合计估算 **0.01276924 元**，预算为 **1 元**，详见旁边的费用 JSON。
余额接口两次读取的差值为 0.00 元；它未反映这批微额扣费，**不将其当作零成本证据**，也不公开账户余额。

Golden 最多 20 轮、单轮输出最多 2048、累计预算 30000 token；三次契约各 15000 token。
发请求前按 UTF-8 请求字节数加 1024 预留输入，剩余预算不足则不发下一次；每轮按服务端 usage 扣减。
按高峰最高单价 8 元/百万 token 预留，本批上限 0.60 元；不在运行器内部重试，Golden 的 `JOB_MAX_ATTEMPTS=1`。
这是一批验收的预算控制，不是跨任务共享钱包；后续实验要重新批准总预算、核价，并按当时模型配置跑。

默认种子按高峰美元价保守估算，并记录来源日期；#145 的独立配置记录的是本次非高峰价。
契约测试复用默认高峰价，因此其结果中的美元估算更保守；费用 JSON 统一按本次非高峰实际时段重算人民币。

## 测试与复核

- 无付费 MiniAgent 测试：**30 passed**，覆盖四工具、路径限制、预算、截止时间、逐轮 usage、缓存成本、故障映射、超时后保留补丁、模型文本不能伪造服务端错误。
- 真实模型契约：**5 passed, 1 skipped in 14.31s**。第 4 条“指定修改某个受保护文件”按套件约定跳过；其原始 diff 保留行为在 sandbox 测试中单独验证。
- `make check`：lint、format、mypy、模块边界全部通过，**2021 passed, 3 skipped, 91 deselected**。三个 skip 为两条既有契约可选项，以及隔离 worktree 没有官方数据缓存的一条测试；真实模型测试不在默认检查中。
- Worker 启动前 `ps -eo args | grep '[a]pp\.worker'` 为空；结束日志有 `worker_stopped`，再次查询为空。没有重灌与集成测试交叉运行；完整检查只连接 `bench_test`。

复核数据库（在此分支的 backend 目录）：

`uv run --no-sync python -m cli.experiment status --run 145`

重跑不付费检查（仓库根目录）：

`make check`

真实契约会花钱，不应并入普通 CI；下次获得费用授权后才手动运行：

`cd backend && uv run --no-sync pytest tests/contract/test_miniagent_runner.py -q`

本次原始证据在主仓库 `var/artifacts/runs/145/tasks/314/attempt-1/`，检查日志在此 worktree 的 `var/miniagent-acceptance/`；原始制品、密钥、余额文件和虚拟环境均不提交。
旁边的证据 JSON 保存运行参数、关键文件摘要、制品摘要与费用汇总，便于检查这次未提交代码的具体内容。

## 后续

用户已授权本分支提交、推送、开 PR，之后仍需 review 合并。2026-09-19 重新核对剩余任务后，建议先验收并补齐 **E9-T3 稳定性加固**，再完成 **E5-T5 平台成本估算**、核对 E8-T4 校准集覆盖情况和模型配置，之后进入 **E10-T4 最终实验**；每张新卡开工前另列 AC 确认，付费实验另批总预算。E6-T3/T4 抽检统计与 E9-T4 性能报告仍需安排，不因采用规则归因而省略。PR #107（中文题面）仍需按最终实验所用 main 状态安排合并；LlamaFactory 半成品不在本卡范围。
