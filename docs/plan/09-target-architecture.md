# 20 Target Architecture Diagrams

## 20.1 System Context（系统上下文）

```mermaid
flowchart TB
    subgraph EXT["外部世界"]
        GH["GitHub API<br/>Issue / PR / 源码"]
        LLM["LLM 提供方<br/>Anthropic / DeepSeek / Qwen ..."]
        HF["SWE-bench Verified<br/>官方数据集与镜像"]
    end

    subgraph USERS["使用者"]
        DEV["开发/研究者<br/>建集·跑评测·看报告"]
        REV["抽检员<br/>盲检归因"]
    end

    subgraph SYS["AI Coding Agent 评测基准平台"]
        WEB["Web 前端<br/>Next.js 16"]
        API["API 服务<br/>FastAPI"]
        WK["评测 Worker<br/>×N 进程"]
        DB[("PostgreSQL")]
        ART[("Artifact Store<br/>Local / MinIO")]
        DKR["Docker Engine<br/>Agent 容器 + 测试容器"]
    end

    DEV --> WEB
    REV --> WEB
    WEB --> API
    API --> DB
    API --> ART
    WK --> DB
    WK --> ART
    WK --> DKR
    WK -.任务挖掘.-> GH
    WK -.导入校准集.-> HF
    DKR -.仅 Agent 阶段·域名白名单.-> LLM
    WK -.归因 Judge.-> LLM
```

## 20.2 Runtime Architecture（运行时架构）

```mermaid
flowchart LR
    subgraph FE["前端 :3000"]
        UI["Dashboard · Benchmarks · Runs<br/>Leaderboard · Analysis · Review"]
    end

    subgraph BE["API 服务 :8000"]
        R["api/ 路由层"]
        SVC["evaluation · benchmark · report"]
    end

    subgraph WORKERS["Worker 进程组"]
        Q["job_queue 领取<br/>FOR UPDATE SKIP LOCKED"]
        SEM["双层信号量<br/>agent_sem / sandbox_sem"]
        ORCH["EvaluationTaskRun 状态机"]
        RUN["runner/<br/>Mock·Oracle·Aider·ClaudeCode·MiniAgent"]
        SBX["sandbox/<br/>工作区物化·容器限额·网络策略"]
        JDG["judge/<br/>补丁归一化·报告解析·F2P/P2P"]
        ATR["attribution/<br/>规则→特征→LLM"]
    end

    subgraph DATA["数据层"]
        PG[("PostgreSQL<br/>业务状态 + 队列")]
        AS[("ArtifactStore<br/>日志·补丁·轨迹·报告")]
        IMG[("Docker 镜像仓库<br/>bench-base / bench-env / bench-agent")]
        MIR[("Git bare mirrors")]
    end

    UI -->|REST + 轮询| R
    R --> SVC
    SVC --> PG
    SVC --> AS
    SVC -->|入队| PG

    Q --> PG
    ORCH --> Q
    ORCH --> SEM
    SEM --> RUN
    SEM --> SBX
    RUN --> SBX
    SBX --> IMG
    SBX --> MIR
    SBX --> JDG
    JDG --> ATR
    ORCH --> PG
    ORCH --> AS
```

## 20.3 Evaluation Sequence（单题评测时序）

```mermaid
sequenceDiagram
    autonumber
    participant W as Worker
    participant DB as PostgreSQL
    participant FS as Workspace
    participant AC as Agent 容器
    participant TC as 测试容器
    participant AS as ArtifactStore

    W->>DB: 领取 job (SKIP LOCKED) → PREPARING
    W->>FS: git archive base_commit → /ws
    W->>FS: git init + 单次提交（历史剥离）
    Note over W,FS: 防泄题：Agent 看不到 base 之后任何提交

    W->>DB: → AGENT_RUNNING（占 agent_sem）
    W->>AC: 启动容器（限额 + 出站白名单代理）
    W->>AC: stdin ← AgentTaskInput(JSON)
    AC-->>W: stdout → AgentRunResult(JSON) + 轨迹
    W->>AS: 存 stdout/stderr/trajectory
    Note over W,AC: 硬超时 → docker stop/kill；AGENT_TIMEOUT 归属 Agent

    W->>DB: → PATCH_CAPTURED（释放 agent_sem）
    W->>FS: git diff → 剔除受保护路径 → NormalizedPatch
    W->>AS: 存补丁

    W->>DB: → TESTING（占 sandbox_sem）
    W->>FS: 重新 archive 纯净工作区
    W->>FS: apply agent_patch → 强制还原受保护路径 → apply test_patch
    W->>TC: 启动测试容器（--network none）
    TC-->>W: junit.xml + stdout
    W->>AS: 存测试报告与日志

    W->>DB: → JUDGING：逐用例状态 → F2P/P2P → agent_outcome
    W->>DB: 写 test_results 明细
    W->>DB: → ANALYZING：规则前置；模糊则入 ATTRIBUTE 队列（异步）
    W->>DB: → COMPLETED（释放 sandbox_sem）
```

## 20.4 EvaluationTaskRun State Machine（一次评测的状态流转）

```mermaid
stateDiagram-v2
    [*] --> QUEUED
    QUEUED --> PREPARING
    PREPARING --> AGENT_RUNNING
    AGENT_RUNNING --> PATCH_CAPTURED
    PATCH_CAPTURED --> TESTING
    TESTING --> JUDGING
    JUDGING --> ANALYZING
    ANALYZING --> COMPLETED
    COMPLETED --> [*]

    AGENT_RUNNING --> COMPLETED: AGENT_TIMEOUT<br/>存补丁但不跑测试，判 UNRESOLVED
    AGENT_RUNNING --> COMPLETED: AGENT_RUNTIME_ERROR<br/>重试耗尽后判 UNRESOLVED
    PATCH_CAPTURED --> COMPLETED: 正常退出且补丁为空<br/>判 EMPTY_PATCH
    TESTING --> COMPLETED: 补丁导致的 TEST_TIMEOUT<br/>判 UNRESOLVED
    TESTING --> COMPLETED: PATCH_APPLY_FAILED<br/>判 INVALID_PATCH

    PREPARING --> FAILED: ENV_BUILD_FAILED / WORKSPACE_ERROR<br/>AI 未启动 → NOT_ATTEMPTED
    AGENT_RUNNING --> FAILED: AGENT_AUTH_ERROR<br/>外部服务问题 → NULL
    TESTING --> FAILED: SANDBOX_ERROR / OOM_KILLED<br/>AI 已启动 → NULL
    TESTING --> FAILED: 环境导致的 TEST_TIMEOUT<br/>对照组也超时 → NULL
    JUDGING --> FAILED: TEST_DISCOVERY_ERROR / HARNESS_ERROR<br/>解析器问题 → NULL
    ANALYZING --> COMPLETED: 归因失败不影响判定结论

    QUEUED --> CANCELLED
    PREPARING --> CANCELLED
    AGENT_RUNNING --> CANCELLED
    TESTING --> CANCELLED

    FAILED --> [*]
    CANCELLED --> [*]

    note right of COMPLETED
        终态只有三个：COMPLETED / FAILED / CANCELLED
        没有 TIMEOUT 终态——超时类型记在 infra_outcome 里

        COMPLETED = 拿到了可信结论（哪怕结论是"没修好"）
        FAILED    = 没拿到可信结论

        FAILED 时 agent_outcome 分两种：
          AI 从未启动     → NOT_ATTEMPTED
          AI 启动后才故障 → NULL
        判据：agent_started_at 是否为空

        合法组合共六种，见协议 C-68
        重试 = 新建一条 attempt_no+1 的记录，不回退状态
        统计只看被标记为 canonical 的那一次
    end note
```

## 20.5 Benchmark Task State Machine（任务生命周期）

```mermaid
stateDiagram-v2
    [*] --> DISCOVERED: GitHub 挖掘
    DISCOVERED --> CANDIDATE: 结构可解析（含 test_patch + code_patch）
    CANDIDATE --> REJECTED: LLM 预筛 score<2 / 疑似泄题
    CANDIDATE --> VALIDATING: 预筛通过
    CANDIDATE --> REVIEW_REQUIRED: 2≤score<4

    VALIDATING --> VALID: 8 步验证全通过
    VALIDATING --> INVALID: F2P_NOT_FAILING / GOLD_NOT_FIXING / ENV_UNBUILDABLE ...
    VALIDATING --> REVIEW_REQUIRED: 边界情况（F2P 数异常/接近超时）

    REVIEW_REQUIRED --> VALID: 人工确认
    REVIEW_REQUIRED --> INVALID: 人工否决

    VALID --> PUBLISHED: 进入 benchmark_set 快照
    PUBLISHED --> QUARANTINED: 周期复验失败 / 抽检标记 TASK_DEFECT
    QUARANTINED --> VALID: 修复后复验通过

    INVALID --> [*]
    REJECTED --> [*]
    PUBLISHED --> [*]

    note right of PUBLISHED
        已发布版本的快照不受后续变更影响
        （benchmark_set_items 冻结 content_hash）
    end note
```
