# 10 Sandbox Design

## 10.1 三种架构对比

| 维度 | **A：Agent 与测试同容器** | **B：Agent 在宿主机，测试在容器** | **C：Agent 容器 + 独立测试容器**（推荐） |
|:---|:---|:---|:---|
| 判定纯净性 | ✗ Agent 装的包/改的环境污染测试结果 | △ 工作区可能被宿主机污染 | ✓ 测试容器从纯净镜像重建 |
| 安全隔离 | ✓ | ✗ Agent 在宿主机可任意执行命令 | ✓ |
| Claude/Codex CLI 兼容 | ✓ | ✓✓（鉴权最容易） | ✓（需把 CLI 装进镜像 + 注入凭据） |
| 鉴权复杂度 | 中 | 低 | 中（env 注入或只读挂载凭据文件） |
| 网络管控 | ✗ 一个容器无法同时"能出网"和"不能出网" | △ | ✓ Agent 容器白名单出网，测试容器 `--network none` |
| 并行资源控制 | 中 | ✗ 宿主机无限制并发易打爆 | ✓ 两类容器分别限流 |
| 调试便利 | 中 | ✓ | 中（需 `--keep-workspace` 调试开关） |
| 实现工作量 | 小 | 小 | **中**（多一次容器编排 + 工作区传递） |
| **4 周风险** | 低但**结论不可信** | 低但**不安全不可复现** | **中，可控** |

## 10.2 决策：Architecture C（双容器，工作区通过卷传递）

```
┌── PREPARING ────────────────────────────────────────────┐
│ 宿主机: git archive base_commit → /var/lib/bench/ws/{run}/workspace
│         git init + 单次提交（历史剥离，防泄题）
└─────────────────────────────────────────────────────────┘
                    │ 绑定挂载 rw
┌── AGENT_RUNNING ──▼─────────────────────────────────────┐
│ 容器: {env_image}+{agent_layer}                          │
│ 挂载: /workspace (rw)                                    │
│ 网络: bench-egress 网络 + 域名白名单代理（只放行 LLM API）│
│ 限额: --cpus=1 --memory=1536m --pids-limit=512 --read-only=false
│       --cap-drop=ALL --security-opt=no-new-privileges
│       --tmpfs /tmp:size=512m  -u 非 root
│ 超时: agent_timeout_s（docker stop → kill）              │
└──────────────────────────────────────────────────────────┘
                    │
┌── PATCH_CAPTURED ─▼─────────────────────────────────────┐
│ 宿主机: git -C ws diff → 剔除 protected_paths → 归一化    │
│         → NormalizedPatch 制品（不再使用该 workspace）    │
└──────────────────────────────────────────────────────────┘
                    │
┌── TESTING ────────▼─────────────────────────────────────┐
│ 全新工作区: 再次 git archive base_commit（纯净！）        │
│ 依次施加: agent_patch → test_patch                       │
│ 容器: {env_image}（不含 agent 层）                        │
│ 网络: --network none                                     │
│ 限额: --cpus=1 --memory=1536m --pids-limit=512           │
│ 执行: test_command，只跑 F2P ∪ P2P 子集，输出 junitxml     │
│ 超时: test_timeout_s                                     │
└──────────────────────────────────────────────────────────┘
```

**"再次 archive 出纯净工作区"是本设计的关键。** 它保证：Agent 在工作区里 `pip install` 了什么、生成了什么临时文件、改了什么受保护文件，**统统不会影响测试**。测试看到的只有：base 代码 + 它的补丁 + 官方测试。

## 10.3 沙箱能力清单（对应 FR-07）

| 能力 | 实现 | 验证方式 |
|:---|:---|:---|
| 工作区隔离 | 每个 task_run 独立目录 + 绑定挂载 | IT |
| CPU 限额 | `--cpus`（cgroup v2 cpu.max） | IT：压满 CPU 观察限流 |
| 内存限额 | `--memory` + `--memory-swap` 相同（禁 swap） | IT：分配大数组 → 期望 exit 137 |
| 进程数限额 | `--pids-limit` | IT：fork 炸弹 → 被拒 |
| 磁盘限额 | `--storage-opt size=`（需 overlay2+xfs）；退化方案 = tmpfs 限额 + 事后 `du` 检查 | IT |
| 墙钟超时 | harness 侧计时 + `docker stop/kill` | IT |
| 网络策略 | Agent：自定义 bridge + 出站代理白名单；测试：`--network none` | IT：容器内 curl github.com 应失败 |
| 文件系统策略 | `--cap-drop=ALL`、`no-new-privileges`、非 root 用户、`/tmp` 用 tmpfs | IT |
| 环境变量 | 白名单注入（仅 API Key + 语言/时区），显式清空其余 | UT |
| 确定性 | `TZ=UTC`、`LC_ALL=C.UTF-8`、`PYTHONHASHSEED=0`、`SOURCE_DATE_EPOCH` | E2E 重跑一致性 |
| 清理 | label 标记 + `docker rm -f` + Worker 启动时孤儿回收 | IT |

### 实测验证结论（2026-09-01，Docker 29.7.2 / cgroup v2 / systemd driver / WSL2 Ubuntu 24.04）

E2-T2 的全部验收负例已在开发机上跑通，**沙箱能力无需再做可行性验证，可直接进入实现**：

| 负例 | 实测结果 | 判定 |
|:---|:---|:---|
| `--memory=256m` 下申请 400 MB | `ExitCode=137`，`OOMKilled=true` | ✅ |
| `--pids-limit=32` + fork 循环 | 第 31 个 fork 抛 `BlockingIOError` | ✅ |
| 进程忽略 SIGTERM + `docker stop --time=2` | 2,622 ms 后 SIGKILL，`ExitCode=137` | ✅ 宽限期精确 |
| 容器清理 | `docker ps -a` 无残留 | ✅ |
| `--network none` 下连 1.1.1.1:443 | `OSError` | ✅ |
| 容器 → 宿主代理 `<gw>:10808` | REACHABLE | ✅ Agent 容器可访问 LLM API |
| 容器内 `pip install` | 成功 | ✅ ADR-008 镜像构建可行 |

**由此确认的一条实现细节**：`OOM_KILLED` 与 `AGENT_TIMEOUT` 的退出码都是 137，**必须靠 `.State.OOMKilled` 字段区分**，不能靠退出码。

**明确不做（避免过度设计）**：gVisor/Kata 等强隔离运行时、seccomp 自定义 profile、用户命名空间重映射、cgroup 手工编排。理由：被测对象是 Coding Agent 而非恶意样本，Docker 默认隔离 + 上述加固已足够；这些属于 §29 NOT NOW。

## 10.4 镜像分层与缓存策略（决定 MET-02 成败）

```
Layer 1  bench-base:py3.11             OS + build-essential + git + uv/pip + 常用编译头
         （1 个，构建 1 次，~5 min）
   │
Layer 2  bench-env:{environment_id}     仓库 mirror + 依赖安装 + pip freeze lock
         （每个 environment_spec 1 个，构建 2–8 min，全项目约 10–25 个）
   │
Layer 3a bench-agent:{env}-{agent}      在 Layer 2 上加 Agent CLI（aider/claude/qwen）
         （env × agent 组合，构建 <1 min，可用同一 agent 层复用）
   │
Layer 3b 运行期容器                      从 Layer 2/3a 起容器，挂载工作区，不再装任何东西
```

### 为什么这是硬需求，不是优化

| 方案 | 单次运行的依赖成本 | 300 次总计 |
|:---|:---|:---|
| 每题运行时 `pip install` | 60–180 s | **5 – 15 小时**（单这一项就爆掉 6h 预算） |
| 仓库级预建镜像 | **0 s**（已在镜像里） | **0**（一次性构建 10–25 个镜像 ≈ 1–3 小时，且可在实验前夜完成） |

**结论：预建镜像是 MET-02 的必要条件。** 写进 ADR-008。

### 环境规格分桶
`environment_spec = (repo, python_version, dependency_snapshot_commit, install_command, test_command, protected_paths)`，`environment_id = repo + '__' + short_hash(spec)`。
同一仓库若跨越大版本导致依赖不兼容，则产生第 2 个 env spec。目标：**平均每仓库 ≤2 个 env**，全项目镜像总数 ≤25，总磁盘 ≤80 GB（本机 920 GB 可用，充裕）。

### 镜像治理
- 全部按 **digest** 记录进 `environment_specs.image_digest` 与运行 manifest；
- 提供 `bench images build --dataset benchmark-cn-v1` 一键预热命令；
- 提供 `bench images gc` 清理无引用镜像；
- 磁盘水位监控（<15% 时拒绝新建 run 并告警）。

## 10.5 出站网络白名单代理
Agent 阶段需要访问 LLM API，但**绝不能**访问 github.com（会搜到原 PR）。
实现：一个 tinyproxy/mitm 风格的轻量 HTTP(S) 代理容器，接在 `bench-egress` 网络上，只放行配置的域名（如各 LLM 提供方 API 域名）。Agent 容器注入 `HTTP_PROXY/HTTPS_PROXY/NO_PROXY`，并 `--dns` 指向不解析其他域名。
**降级方案（若代理调试超时）**：Agent 容器直接联网，但在 `AgentTaskInput` 中不含 repo URL/PR 编号，并在归因阶段用规则检测轨迹中是否出现 `github.com/<repo>/pull` 访问 → 标记 `POSSIBLE_LEAK` 并从统计中剔除。风险披露写进报告。

## 10.6 Docker 客户端与 Day-0 阻塞
- 平台通过 **docker SDK for Python** 操作本机 daemon（`/var/run/docker.sock`）。
- API 服务与 Worker 用 docker compose 起；**Worker 需要挂载 docker.sock**（DooD 模式，非 DinD）。这引入宿主机权限暴露——在单机实训环境可接受，但必须在部署文档中明示，并把 Worker 容器限制为非公开端口。
- **Day-0 已完成（2026-09-01）**：WSL2 内安装原生 docker engine（Docker 29.7.2 + Compose v5.5.0），systemd 托管、开机自启、免 sudo 可用；`.wslconfig` 调至 16 vCPU / 11 GiB。

**新增的环境约束（实测，务必写进部署文档）**：开发网络需经 Windows 侧 VPN 代理出网，而 **dockerd 不继承 shell 的代理环境变量**，必须单独配置两处，缺任何一处都会表现为"拉不动镜像"：

| 配置点 | 位置 | 作用 |
|:---|:---|:---|
| dockerd 代理 | `/etc/systemd/system/docker.service.d/http-proxy.conf` | `docker pull` 走代理 |
| Registry 镜像源 | `/etc/docker/daemon.json` 的 `registry-mirrors` | Docker Hub 走国内直连，不占 VPN 带宽 |
| 客户端/容器代理 | `~/.docker/config.json` 的 `proxies.default` | `docker build` 与 `docker run` 时向容器注入代理，供 pip 安装与 Agent 访问 LLM API |

注意代理地址用的是 WSL NAT 网关（`ip route show default`），该 IP **在 `wsl --shutdown` 后可能变化**，届时上述三处需同步更新——见风险 R18。
另：实测镜像拉取速率约 **4 MB/s**，规划镜像预热时间时按此估算。

**与 Windows 侧 Docker Desktop 共存的约束（实测确认，务必写进部署文档）**

本机同时装有 Docker Desktop（供其他 Windows 项目使用）。二者可以共存，但有硬性前提：

| 约束 | 说明 |
|:---|:---|
| **禁止对本发行版启用 Docker Desktop 的 WSL 集成** | 集成会把其 CLI 前置进 PATH 并接管 `/var/run/docker.sock`，导致 CLI 指向另一个 daemon。症状是镜像与容器"凭空消失"、正在跑的评测整体表现为失败，排查成本极高。当前状态：`/mnt/wsl/docker-desktop*` 不存在 → 集成已关闭 ✅ |
| **最终实验期间退出 Docker Desktop** | 所有 WSL2 发行版共用同一个工具 VM，其内存受 `.wslconfig` 的 `memory` 统一约束。Docker Desktop 运行时会占用本应留给测试容器的额度，直接影响 `SANDBOX_CONCURRENCY` 的可用上限（关联 R07） |
| **端口避让** | compose 发布的端口需避开 Docker Desktop 侧项目常用端口 |
| **daemon 自检** | `docker info --format '{{.Name}} {{.DockerRootDir}}'` 应返回本机名与 `/var/lib/docker`；`docker context` 必须停留在 `default`，切勿切到残留的 `desktop-linux` |

上述四条应作为 `scripts/check_env.py` 的检查项，在每次启动 EvaluationRun 前自动校验——**在长跑实验开始前失败，远好过跑到一半才发现连错了 daemon**。

---

## 10.7 工作区物化的实现决策（E2-T1 落地回填，2026-09-04）

代码在 `backend/app/sandbox/{git_cli,mirror,workspace}.py`。§10.2 的流程图和 §7.2(1)
的四步没有变，下面是实现时才浮出来的四个问题和处理方式。

### (1) 基线忽略清单写 `.git/info/exclude`，不是工作区根的 `.gitignore`

ADR-007 的风险缓解写的是"工作区内置 `.gitignore` 基线"。实现时发现不能照字面做：
仓库自己往往就有一个 `.gitignore`，我们再写一个要么覆盖它、要么和它打架，
而且那是**对被跟踪文件的改动**——工作区的树哈希会因此和 base 树对不上。

`.git/info/exclude` 是 git 专门给"仓库本地、不入库"的忽略规则准备的位置，
效果一样，且不动工作树。清单本身放在 `workspace.DEFAULT_WORKSPACE_IGNORE`。

**挑选原则：只挡确定是机器生成的东西，宁可漏挡不可错挡。** 漏挡的代价是补丁里多点噪声；
错挡的代价是 Agent 真写的源文件被悄悄丢掉、判成"没修好"，而且不报错。
所以 `build/`、`dist/` 这种"通常是产物、但也可能是仓库里真实的源码目录"不进清单，
留给 E3-T3 的补丁归一化按大小和扩展名过滤。

### (2) base 提交必须 `git add --all --force`

不加 `--force` 的话，仓库里**本来就跟踪着**的文件只要命中基线清单（比如一个被跟踪的
`debug.log` 命中 `*.log`）就会被漏掉。工作区因此比 base 少一个文件，没有任何报错。
忽略规则只应该作用于物化之后新出现的文件。

### (3) 物化后自查树哈希，`export-ignore` 会被当场拦下

物化完对比两个值：工作区 `HEAD^{tree}` 与镜像里 `<base_commit>^{tree}`。
git 的树哈希覆盖每个文件的路径、权限位和内容，相等就说明一处不差。

这条自查挡住的是一类很阴的失败：仓库的 `.gitattributes` 里如果写了
`tests/ export-ignore`，`git archive` 会**静默跳过**这些路径。工作区少了 `tests/`，
一路跑到测试阶段才报"找不到用例"，排查方向全在测试执行器上。现在它在物化这一步就
失败，错误消息直接点名缺了哪些文件、以及 `export-ignore` 这个原因。

带子模块的仓库同样会在这里被识别出来（gitlink 条目不会出现在工作区里）。

### (4) 所有 git 调用屏蔽开发机的全局配置

`git_cli.run_git()` 统一把 `GIT_CONFIG_GLOBAL` / `GIT_CONFIG_SYSTEM` 指向 `/dev/null`，
并设 `GIT_TERMINAL_PROMPT=0`、`TZ=UTC`、`LC_ALL=C.UTF-8`。

理由是可复现（NFR-02）：开发机上一句 `core.autocrlf=true` 就会让 `git add` 改写换行符，
同一个 commit 在两台机器上物化出不同的树哈希；`commit.gpgsign=true` 更直接，
签名失败则物化整个报错。`GIT_TERMINAL_PROMPT=0` 则是防止无人值守时 git 停下来问密码。

**只覆盖这几个变量，不清空环境**：`HTTP_PROXY`/`HTTPS_PROXY` 要原样传下去，
这台机器上 git 出网靠它们。副作用是 `~/.gitconfig` 里的 `http.proxy` 不再生效，
需要改用环境变量——`scripts/check_env.py` 会检查 git ≥ 2.32（`GIT_CONFIG_GLOBAL` 的最低版本）。

### 顺带确定下来的两件事

- **base 提交的 SHA 是确定的**：提交人和作者/提交时间都写死成常量，
  于是"两次物化结果一致"从"目录树相同"升级成"整个 `.git` 都相同"。
- **工作区里预置了 git 身份**（写在 `.git/config`）：有些 Agent 干完活会自己
  `git commit`，没有身份会撞上 "Please tell me who you are" 白烧轮次。
  代价是 E3-T3 抓改动时**必须用 `git diff <base_sha>`**，不能用裸的 `git diff`——
  Agent 提交过之后裸 diff 是空的。`Workspace.base_sha` 就是给这一步用的。
