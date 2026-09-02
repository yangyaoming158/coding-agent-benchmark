# 26 Acceptance Criteria（验收标准）

## 26.1 交付物验收

| ID | 交付物 | 验收判据（可执行） |
|:---|:---|:---|
| DEL-01 | 平台源代码（含中文注释） | 干净机器 `docker compose up` 后全部服务健康；lint/type/test 三绿；公共模块与领域枚举有中文注释；import-linter 边界规则通过 |
| DEL-02 | 100 题评测任务集 | 平台内 `PUBLISHED` 任务 ≥100（自建中文 ≥40）；Oracle 解决率 100%、Noop 0%；数据集质量报告含来源/语言/难度/漏斗数据 |
| DEL-03 | ≥3 Agent 对比报告 | 同一 `benchmark_set` 版本下 ≥3 个 `agent_config` 的完整运行；报告含解决率、成本、耗时、分难度/分语言分面 |
| DEL-04 | 失败归因分析 | 8 类分布图 + Agent×类别热力图 + Top-N 失败案例（含补丁/日志/轨迹链接）；抽检准确率与 κ |
| DEL-05 | 性能报告 | makespan、有效并发时序、各阶段 P50/P95、external_wait 占比、成本分布、infra 失败率、异构机器外推 |
| DEL-06 | 部署文档 | 由**未参与开发的同学**照文档在干净环境部署成功（这是验收方式，不是形容词） |

## 26.2 量化指标验收（含降级判据）

| 指标 | 达标 | 降级达标 | 不达标时的交付要求 |
|:---|:---|:---|:---|
| MET-01 复现 ≤5pp | Harness Replay 逐实例一致率 ≥98% **且** 解决率偏差 ≤5pp | Live 子集 n≥50 点估计偏差 ≤10pp 且落在 CI 内 | 出具偏差分析：逐实例差异清单 + 根因（镜像差异/解析差异/任务差异） |
| MET-02 ≤6h | 300 次 makespan ≤6h | 平台 makespan（扣除 external_wait）≤6h | 出具容量模型 + 瓶颈归属 + 异构机器外推 |
| MET-03 并行 ≥8 | 有效并发时序 P50 ≥8 | 峰值 ≥8 | 说明硬件限制并给出 16C/32G 下的实测/外推 |
| MET-04 ≥85% | 盲检 ≥50 例准确率 ≥85% | ≥75% 且规则类准确率 ≥95% | 出具混淆矩阵 + 分类体系修订说明 |
| MET-05 ≥100 题 | ≥100 且自建中文 ≥60 | ≥100（自建中文 ≥40） | 出具漏斗数据说明产出率 |
| MET-06 ≥3 Agent | ≥3 主流 CLI + 1 自研 | 2 主流 + 1 自研 | 说明受阻 Agent 与具体阻塞点 |

**关键纪律**：任何降级都必须在报告中**主动披露**，附数据与原因。评测平台的核心品格是诚实——隐瞒降级比降级本身严重得多，答辩时也一定会被问出来。

## 26.3 答辩演示脚本（5 分钟主线）
1. 打开 Benchmark Detail：展示一道中文 Issue 任务、F2P/P2P 清单、验证证据（30s）
2. 新建一次 Run：MockAgent × golden-tasks，实时看状态机流转（60s）
3. 打开 Task Run Detail：补丁 diff、逐用例结果、Agent 轨迹（60s）
4. **防作弊演示**：用"改测试的 Mock Agent"跑一次，展示其改动被剔除、判定仍为 UNRESOLVED（60s）
5. Leaderboard：3 个真实 Agent 在 cn-v1 上的解决率/成本/耗时对比 + 成本-解决率散点（60s）
6. Failure Analysis + 盲检页：一个真实失败案例的归因与人工纠偏（30s）

**兜底**：全程录屏备份；数据库预置一份完整实验数据的 dump，网络/账号故障时用预置数据演示。

---

# 27 Testing Strategy

## 27.1 分层

| 层级 | 范围 | 工具 | 数量目标 | 何时跑 |
|:---|:---|:---|:---|:---|
| **Unit** | 报告解析、test_id 归一化、补丁归一化、Judge 真值表、状态机不变式、枚举一致性、content_hash 稳定性 | pytest | ~80 用例 | 每次提交 |
| **Integration** | ArtifactStore 契约（Local/MinIO 各跑一遍）、队列租约与回收、DB 迁移升降级、API 契约 | pytest + testcontainers/本地 PG | ~30 用例 | 每次提交 |
| **Sandbox** | 资源限额四条负例（OOM/fork 炸弹/超时/断网）、清理与孤儿回收、工作区历史剥离 | pytest（标记 `@pytest.mark.docker`） | ~12 用例 | 每日 + PR |
| **Agent Adapter** | 6 条契约测试 × 每个适配器（用最便宜模型或 Mock 端点） | pytest（标记 `@pytest.mark.agent`） | 6×N | 适配器变更时 |
| **Benchmark Validation** | 6 种坏任务被正确拒绝；Golden 任务被正确接受 | pytest | ~10 用例 | 每日 |
| **E2E** | Golden×Mock 全链路 → 落库 → API → 前端能看到；Oracle=100%；Noop=0% | pytest + Playwright（1 条主流程） | ~5 场景 | 每日 |
| **Performance** | Pilot 30×3 makespan；并发压测；内存峰值 | 自建 harness + 指标导出 | — | W3、W4 |
| **Security/防作弊** | 改测试被剔除；`git log` 无未来提交；测试容器无网络；gold_patch 不出现在 Agent 输入中 | pytest | ~6 用例 | 每日 |

## 27.2 三条"哨兵测试"（本项目最重要的测试）

所谓哨兵测试，就是像哨兵一样守在门口的检查：它们不测某个具体功能，而是回答一个更根本的问题——**这个数据集和这套判定引擎，整体上还可信吗？**
1. **Oracle 哨兵**：用每道题的官方正确补丁跑整个数据集，**解决率必须是 100%**。只要不是 100%，就说明要么有坏题，要么判定引擎有 bug。**每次发布数据集前必须跑，作为发布门槛。**
2. **Noop 哨兵**：`NoopRunner × 整个数据集 → 解决率必须 = 0%`。非 0 说明有任务的 F2P 在 base 上就通过。
3. **确定性哨兵**：同一 `(task, patch)` 重判 3 次，逐用例状态必须完全一致。

这三条把"基准是否可信"变成了**可自动化验证的断言**，而不是靠人肉相信。

## 27.3 测试数据管理
- Golden Tasks 作为 fixture 进仓库（体积小、无外部依赖）；
- 真实测试报告（junit.xml / pytest 输出）录制 10 份进 `tests/fixtures/reports/`，覆盖 pytest 各种输出形态；
- 禁止单元测试依赖网络与真实 LLM；LLM 归因用录制响应（VCR 风格）回放。

## 27.4 CI 策略（务实）
- PR：unit + integration（不含 docker 标记），<3 分钟；
- 每日夜间：全量含 docker/e2e/benchmark-validation；
- Agent 契约测试手动触发（消耗额度）。

---

# 28 Risk Register

概率/影响：H/M/L。按"风险暴露度"排序。

| ID | 风险 | P | I | Mitigation（预防） | Fallback（发生后） |
|:---|:---|:-:|:-:|:---|:---|
| ~~R00~~ | ~~本机 Docker 不可用 / WSL 资源受限~~ **已于 2026-09-01 关闭**：Docker 29.7.2 就绪，`.wslconfig` 调至 16 vCPU / 11 GiB，E2-T2 七条负例全绿 | ~~H~~ | ~~H~~ | Day-1 在 WSL 内装原生 docker engine（不用 Docker Desktop，省 1–2 GB）；`.wslconfig` 调至 `processors=16` / `memory=12GB` 后 `wsl --shutdown`；双层并发模型适配小内存 | 用 `P_agent=6, P_sandbox=3` 跑，实验分批；最终实验日按小时租云 VM（16C/32G ≈ ¥20–50 一天） |
| **R10** | **100 题构建时间过长 / 产出率低** | **H** | **H** | W1 就定仓库、W1D3 起后台常驻挖掘与验证；收敛到 8–15 精选仓库；验证并行化 | 用官方 Verified 题库补齐；底线是总量 100 道、自建中文 40 道；公开各环节的通过率数据 |
| **R13** | **公开 Agent 结果无法复现（MET-01）** | **H** | **M** | 采用 Harness Replay 主口径（§4.5 Plan A）；Live 子集作辅证并报 CI | 出具偏差分析报告，把"为什么不可复现"本身作为研究结论（这是有价值的结论） |
| **R12** | **6 小时指标不现实** | **M** | **H** | 预建镜像（必要条件）；硬超时；只跑 F2P∪P2P 子集；Pilot 回代 | 分列平台 makespan 与 external_wait；给异构机器外推；降级为 100×2+30×1 |
| **R09** | **真实评测成本过高** | **M** | **H** | 主流 CLI 走订阅；另两个 Agent 用国产低价模型；Pilot 先测 token 均值；设 `max_tokens_budget` 硬预算 | 降题量到 60；降 Agent 数到 2；用 MiniAgent+便宜模型替代 |
| **R08** | **Agent API 限流 / 订阅并发上限** | **H** | **M** | 自适应退避 + 按 agent_config 分桶令牌桶；`external_wait_ms` 记账 | 分 Agent 分时段执行；夜间跑；降 `P_agent` |
| **R03/R04** | **Agent CLI 无法在容器内非交互/鉴权** | **M** | **H** | 优先选纯 API Key 鉴权（Aider/MiniAgent 先行）；W1 就做 CLI 非交互调研 | 该 Agent 降级为宿主机执行（**测试仍在纯净容器**），报告标注；或用 MiniAgent+对应模型替代 |
| **R02** | **任务环境无法复现** | **M** | **H** | commit pin + 镜像 digest + 依赖 lock + 测试断网；env spec 分桶 | 该任务标 `INVALID(ENV_UNBUILDABLE)` 剔除；必要时降级为每题镜像 |
| **R06** | **依赖安装慢 / 镜像构建久** | **M** | **M** | 仓库级预建镜像；国内 PyPI 镜像源；构建并行化；提前一晚预热 | 缩减仓库数；剔除重依赖仓库 |
| **R07** | **并行导致资源耗尽** | **M** | **M** | 双层信号量；`--memory` 硬限；磁盘水位检查；孤儿容器回收 | 自动降并发；单题串行重跑失败项 |
| **R11** | **LLM Judge 不稳定 / 归因准确率不足** | **M** | **M** | 规则前置吃掉 55–70%；`temperature=0`；evidence 强制；低置信投票；缓存 | 合并易混类别；降级为规则+人工，报告说明 |
| **R05** | **Agent 只改 workspace 不输出 patch** | **L** | **M** | 已由 ADR-007 设计解决（harness 侧 git diff） | — |
| **R01** | **GitHub 挖掘失败（限流/关联不规范）** | **M** | **M** | GraphQL 批量 + etag 缓存 + 断点续跑；多仓库分散风险 | 人工从仓库 PR 列表半自动构建；提高官方子集比例 |
| **R14** | **测试 ID 归一化 bug 导致大面积假阴性**（新增） | **M** | **H** | 6+ 形态单元测试；Oracle 哨兵会立刻暴露该问题 | Oracle 哨兵是自动化的早期预警，发现即修 |
| **R15** | **任务泄题导致解决率虚高**（新增） | **M** | **H** | 历史剥离 + 出站白名单 + Issue 脱敏 + 三条防作弊测试 | 轨迹检测到访问原 PR → 标记该结果并剔除，报告披露 |
| **R16** | **数据集有坏题导致结论错误**（新增） | **M** | **M** | Oracle/Noop 双哨兵门禁；抽检可标 TASK_DEFECT → QUARANTINED | 隔离坏题，重算历史指标并标注版本 |
| **R17** | **团队并行度不足 / 关键成员缺席**（新增） | **M** | **M** | 关键路径任务不单点（内核由 A 主责、C 备份）；每日站会同步阻塞 | 按 §23"可以砍"清单顺序收缩范围 |
| **R18** | **代理链路脆弱：整条工具链依赖 Windows 侧 VPN，且代理地址是会变的 WSL NAT 网关**（实测新增）。VPN 掉线或 `wsl --shutdown` 后网关变更，会同时打断 shell、dockerd、容器三处代理，症状表现为"网络坏了"而非"IP 变了"，排查耗时 | **H** | **M** | ① 三处代理地址统一由 `ip route show default` 动态派生，禁止硬编码；② 加开机自动重生成（systemd oneshot，`Before=docker.service`）；③ Docker Hub 走 `registry-mirrors` 国内直连，减少对 VPN 的依赖面；④ 长跑实验前用 `scripts/check_env.py` 做前置体检 | 实验中断后由断点续跑恢复（E9-T3）；镜像提前预热，使实验期不再依赖拉取 |

---

# 29 NOT NOW List（明确不做）

| 项 | 为什么现在不做 | 什么时候才值得做 |
|:---|:---|:---|
| Kubernetes / 容器编排平台 | 单机 8–16 核，compose 足够；K8s 只增加部署与调试成本 | 需要跨机 50+ 并发时 |
| 微服务拆分 | 见 ADR-001 | 团队 >10 人且需独立发布时 |
| 分布式 Worker / 跨机调度 | 单机并发已能满足 MET-03 | makespan 成为硬瓶颈且已有多机 |
| 消息队列中间件（Kafka/RabbitMQ） | Postgres 队列已满足全部语义 | 每秒千级作业时 |
| 自研容器运行时 / gVisor / Kata | 被测对象是 Coding Agent 不是恶意样本 | 面向不可信第三方开放提交时 |
| 自研 Git 平台 / LLM Gateway | 完全的重复造轮子 | 永远不 |
| 多租户 SaaS / 完整 RBAC / 支付 | 实训项目单团队使用 | 对外开放服务时 |
| 微调专用 Judge 模型 | 规则前置 + 通用模型已能达 85% | 有 1000+ 标注样本后 |
| SWE-bench 官方全量兼容（500 题全跑） | 时间与成本都不允许 | 校准子集验证通过后，作为后续研究 |
| 10+ Agent 接入 | 每个适配器 1–2 天，边际收益递减 | 平台稳定后逐个增量添加 |
| 全语言支持（Java/Go/JS/Rust） | 每种语言 = 一套环境规格 + 一个报告解析器 | Python 链路完全稳定后，先加 JS |
| WebSocket 实时轨迹流 | 轮询已满足观察需求 | 单次运行超过 30 分钟且需要逐 token 观察时 |
| 轨迹回放播放器 | 时间线视图已够用 | 做教学/演示产品时 |
| pass@k 多次采样 | 成本 ×k | 成本预算充足时 |
| 花哨 UI 动画 / 自定义设计系统 | 对评测结论零贡献 | 永远不（在本项目内） |

---

# 30 Recommended Repository Structure

> **仅规划目录，本轮不创建。**

```
coding-agent-benchmark/
├── README.md
├── docs/
│   ├── plan/                       # 本规划报告
│   ├── architecture.md
│   ├── deployment.md               # DEL-06
│   ├── evaluation-protocol.md      # §6/§7/§9 的对外协议文档（冻结件）
│   └── adr/                        # ADR-001..012 单文件版
├── docker-compose.yml
├── docker-compose.dev.yml
├── Makefile
├── backend/
│   ├── pyproject.toml
│   ├── alembic/versions/
│   ├── app/
│   │   ├── main.py                 # FastAPI 入口
│   │   ├── api/                    # 路由 + 请求/响应模型
│   │   ├── domain/                 # 枚举、值对象、状态机（零外部依赖）
│   │   ├── benchmark/              # schema / validator / mining / dataset
│   │   ├── runner/                 # base.py + adapters/{mock,oracle,noop,aider,claude_code,qwen,mini,replay}.py
│   │   ├── sandbox/                # docker_client / workspace / images / limits / network
│   │   ├── evaluation/             # orchestrator / state_machine / retry
│   │   ├── judge/                  # patch_normalizer / report_parsers / verdict
│   │   ├── attribution/            # rules / features / llm_judge
│   │   ├── report/                 # aggregate / html / markdown
│   │   ├── storage/                # artifact_store: base/local/minio
│   │   ├── infrastructure/         # db / queue / config / logging / ratelimit
│   │   └── worker/                 # __main__.py + handlers/
│   ├── cli/                        # bench 命令：mine/validate/publish/run/images/report
│   └── tests/
│       ├── unit/ integration/ sandbox/ e2e/ fixtures/{golden,reports}/
├── frontend/
│   ├── package.json
│   ├── app/                        # Next.js App Router: /(dashboard) /benchmarks /runs /task-runs /leaderboard /analysis /review /reports
│   ├── components/                 # PatchViewer / TestResultTable / TrajectoryTimeline / charts/
│   ├── lib/                        # api client（openapi-typescript 生成）/ hooks
│   └── types/
├── images/
│   ├── base/Dockerfile             # bench-base
│   ├── envs/{environment_id}/Dockerfile.j2   # 模板生成
│   └── agents/{aider,claude-code,qwen}/Dockerfile.partial
├── datasets/
│   ├── golden/                     # 人工 Golden Tasks（进版本库）
│   └── exports/                    # 数据集导出快照（JSONL）
└── scripts/
    ├── check_env.py                # Day-0 环境自检
    ├── prewarm_images.py
    └── replay_public_results.py    # MET-01 Plan A
```

---

# 31 First Development Batch（明天开始的前 10 个任务，按执行顺序）

| # | 任务 | 负责人 | 工期 | 完成判据（DoD） | 阻塞谁 |
|:--|:---|:--|:--|:---|:---|
| ~~1~~ | ~~**E0-T1 打通 Docker + 确定机器方案**~~ **✅ 已于 2026-09-01 完成** | A | 0.5d | Docker 29.7.2 + Compose v5.5.0，systemd 托管/开机自启/免 sudo；cgroup v2 + systemd driver、`docker info` 无 warning；`.wslconfig` → 16 vCPU / 11 GiB；代理三处配齐；**E2-T2 七条沙箱负例全绿**（§10.3）；结论：无需采购硬件 | ~~全部~~ 已解除 |
| ~~2~~ | ~~**§6 评测语义评审会 + 冻结**~~ **✅ 已于 2026-09-02 冻结** | 全员 | 0.5d | `docs/evaluation-protocol.md` 定稿签字：三个 outcome 维度、状态机、`INFRA_TO_AGENT_MAPPING` 映射表 | E0-T3, E1-T1, E3-T1 |
| ~~3~~ | ~~**E0-T2 仓库骨架与工程规范**~~ **✅ 已于 2026-09-02 完成** | B | 1.5d | `make check` 全绿（75 个测试）；三条 AC 各自有自动化测试而不是口头确认；`make dev` 前后端一起起来且调通；前端类型从 OpenAPI 生成。**分支保护和建 Issue 需在 GitHub 上人工操作** | ~~所有编码任务~~ 已解除 |
| ~~4~~ | ~~**E0-T3 数据库 Schema v1 + 迁移**~~ **✅ 已于 2026-09-02 完成** | B | 1.5d | `upgrade head` / `downgrade base` / 再 `upgrade head` 三步验过；枚举一致性单测直接解析协议原文比对；`python -m cli.seed` 幂等；`alembic check` 无漂移 | ~~E1-T1, E3-T1, E5-T1~~ 已解除 |
| **5** | **E1-T1 Task Schema 冻结 + 校验器** | B | 1d | Golden 任务 JSON 双向序列化；`content_hash` 对字段序不敏感；6 类非法任务被拒并给出可读原因 | E1-T2, E1-T3, E4-* |
| **6** | **E2-T1 工作区物化 + 防泄题** | A | 1d | 物化后 `git log --all --oneline \| wc -l == 1`；两次物化目录树哈希一致；`.gitignore` 基线生效 | E3-T3, E4-T2 |
| **7** | **E2-T2 容器执行器 + 资源限额** | A | 2d | 四条负例测试全绿（OOM / fork 炸弹 / 超时 / `--network none` 断网）；无残留容器 | E2-T3, E4-T2, E1-T3 |
| **8** | **E3-T1 Runner 协议 + 契约测试套件** | C | 1d | `AgentTaskInput`/`AgentRunResult` 模型与 JSON Schema 导出；6 条契约测试可复用于任意适配器 | E3-T2/T4/T5/T6 |
| **9** | **E3-T2 Mock / Oracle / Noop Runner** | C | 0.5d | 6 种行为可配置精确触发（正确/错误/空/超时/非法/改受保护文件） | E4-T4 与全部 E2E |
| **10** | **E8-T1 仓库选型实测与打分表** | B（+D 协助） | 1d | 产出 ≥15 个候选仓库 × (安装耗时/测试耗时/近 2 年可用 PR 数/中文 Issue 比例/许可证) 打分表，定档 8–15 个仓库 | E1-T4, E8-T2/T3 |

**并行安排**：#1→#2 串行必须最先；#3/#4/#5 由 B 串行推进；#6/#7 由 A 并行推进；#8/#9 由 C 并行推进；#10 由 B 在 #5 后穿插（或 D 协助先跑脚本）；D 同期做前端脚手架与 API 类型生成。

**第一周唯一验收目标**：**W1D5 达成 M1 —— `Golden Task × MockAgent → 补丁 → Docker 测试 → RESOLVED` 全链路跑通并在前端可见。**
在此之前，**不要碰**：真实 Agent、GitHub 挖掘的规模化、MinIO、LLM 归因、任何图表美化。
