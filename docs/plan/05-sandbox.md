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

---

## 10.8 容器执行器的实现决策（E2-T2 落地回填，2026-09-04）

代码在 `backend/app/sandbox/container.py`，对外只有一个入口 `run_in_container()`。
§10.2 的双容器流程和 §10.3 的能力清单没有变，下面是实现时才浮出来的几个问题。

### (1) OOM 与超时的判定顺序写死在一个函数里

`classify_outcome()` 先看 `oom_killed` 再看 `timed_out`，顺序不能换（协议 C-19b 第 1 步）。

写成函数而不是散在调用处的 `if`，是因为这两件事的退出码都是 137，判反了不会报错，
只会让排行榜偏低：内存超限按 C-18 要降配重试一次，超时则直接判 AI 没修好。
`test_container_spec.py::test_oom_wins_over_timeout` 用一个"两个标记同时为真"的
结果对象把顺序钉死——容器超内存被杀时，我们的墙钟可能正好也到点，这个组合是真会出现的。

非零退出码在这一层**不翻译**。它在两个阶段的含义相反：测试阶段非零退出是正常的
（有用例失败），Agent 阶段非零退出才算故障，而"Agent 算不算跑成功"要看它 stdout
最后一行的 JSON（Runner 协议）。放进来等于让沙箱层去猜上层的语义。

### (2) 不能开 `auto_remove`

`auto_remove=True` 会让容器一退出就被 daemon 删掉，而 `.State.OOMKilled` 必须在退出
**之后**读——容器没了就读不到，OOM 会被静默当成普通的非零退出。所以删容器手工写在
`finally` 里，并且 `_remove_quietly()` 自己不抛异常：它在 `finally` 里跑，抛出来会把
真正的失败原因（比如"镜像不存在"）盖掉。

### (3) 环境变量白名单：名字不在名单里就报错，不是悄悄丢掉

`build_env()` 拼两部分：固定的确定性变量（`TZ`、`PYTHONHASHSEED` 等，协议 C-37）
加上 `AGENT_ENV_ALLOWLIST` 里的请求项。确定性变量不在白名单里，因此调用方覆盖不了。

遇到不认识的名字**抛异常**。悄悄丢掉的表现是几分钟后 Agent 报 401，
排查时根本想不到是这一层过滤的。反方向的例子是 `GITHUB_TOKEN`：它不在白名单里，
进去了就等于把翻原修复 PR 的钥匙交给了被测 AI。

### (4) 限额验证要从容器内部读 cgroup，不能只看传参

只断言"参数传给了 docker"挡不住字段名拼错或者重构时漏掉一行——容器照样跑起来，
只是不再受限，没有任何报错。cgroup v2 下 `/sys/fs/cgroup/{memory.max,pids.max,cpu.max}`
就是内核实际执行的值，在容器里 `cat` 出来核对，才算限额真的生效。

### (5) 镜像不在本地直接报错，不自动拉

`containers.create()` 不会拉镜像，缺镜像抛 `ImageNotFoundError`。评测跑到一半去拉镜像
会打爆 6 小时预算，也让结果不可复现（ADR-008）。预建镜像是 E2-T3 的事。

### 四条负例的固化结果（Docker 29.7.2 / cgroup v2 / WSL2）

| 负例 | 断言 | 实测 |
|:---|:---|:---|
| `--memory=256m` 下反复写 10 MB 块 | `oom_killed` 为真 → `OOM_KILLED` | ✅ ExitCode=137 |
| `--pids-limit=32` + fork 循环 | 成功 fork 次数 < 上限 | ✅ 第 31 次抛 `BlockingIOError` |
| 忽略 SIGTERM 的死循环 + 2 秒超时 | 到点被杀、容器不残留 | ✅ 宽限期过后升级到 SIGKILL |
| `--network none` 下连 1.1.1.1:443 | 连不出去 | ✅ `Errno 101 Network is unreachable` |

全部 16 条容器用例 12 秒跑完，带 `docker` 标记，`make test-docker` 触发，CI 不跑。

### 顺带撞上的两件事

- **Docker Desktop 会抢 `/run/docker.sock`。** 这台机器上原生 dockerd 由 systemd 托管，
  Docker Desktop 的 WSL 集成启动后会用同一个路径覆盖掉那个 socket，于是 `docker` 命令
  连到的是 Desktop 的引擎（27.5.1），镜像和容器看起来"凭空消失"，而且 Desktop 用的是
  它自己的代理，`docker pull` 直接失败。`sudo systemctl restart docker.socket docker.service`
  能把 socket 抢回来。
  `scripts/check_env.py` 原来靠 `DockerRootDir == /var/lib/docker` 判断"连的是不是原生
  引擎"，**这是个假绿灯**——Desktop 的引擎报的也是这个路径。已改成看 `Labels` 里有没有
  `com.docker.desktop.*`。
- **`docker run` 命令行会把 `~/.docker/config.json` 里的 `proxies` 注入每个容器**，
  Python SDK 不读那份配置。我们走 SDK，所以容器里只有 `build_env()` 拼出来的变量。
  这是有意的：起容器不要改成调命令行，否则代理变量会绕过白名单进到测试容器里。

---

## 10.9 镜像分层构建器的实现决策（E2-T3 落地回填，2026-09-08）

代码在 `backend/app/sandbox/images.py`（纯逻辑）+ `backend/cli/images.py`（落库、落制品）
+ `images/base/Dockerfile` + `images/envs/*.json`。§10.4 的三层结构没有变，
下面是实现时才浮出来的问题和处理方式。

### (1) 第二层不写 Dockerfile，从配方渲染

`images/envs/{environment_id}.json` 是配方（几百字节，进版本库），
`render_env_dockerfile()` 把它渲染成 Dockerfile。8 个仓库最多 16 个环境，
彼此只差四五个变量，写 18 份 Dockerfile 等于改一处公共逻辑要改 18 遍。

**`install_steps` 故意不给默认值。** 一个仓库怎么装是它自己的事实（有的要
`--no-build-isolation`，有的要 `--group`，Golden 题压根不用装），藏进 Python 常量里的话，
三周后没人查得到当初到底跑了什么。空列表是合法的，但必须显式写出来。

第三层不新建文件：`images/{aider,claude-code}/Dockerfile` 的 `FROM` 改成
`ARG BASE_IMAGE=bench-golden:py311` + `FROM ${BASE_IMAGE}`。`make images-aider` 照旧能用，
构建器传 `--build-arg BASE_IMAGE=bench-env:<环境 id>` 就把同一份 Dockerfile 叠到了环境镜像上。

### (2) 走 docker SDK 的经典构建器，事件流直接当构建日志

`client.api.build(..., decode=True)` 吐的是 `{"stream": "..."}` 的事件流，
拿它当日志就不用去解析终端输出，也不会踩"管道吃掉退出码"那一条（§8.8 坑 ①）。

**实测确认它在 Docker 29.7.2 / API 1.55 上仍然可用，缓存也正常**：同一份上下文
第一次 4.71 秒，第二次 0.03 秒、每一步都是 `---> Using cache`。

一个必须显式处理的地方：**出错时流照样正常结束，不抛异常**，只是多一条
`{"error": ...}` 事件。不检查它的话，构建失败会被当成成功 —— 和 §8.8 坑 ① 是同一类问题，
失败信号在一条没人看的通道上。

### (3) 两层缓存，而且镜像标签里不能有时间戳

- **配方哈希**（`bench.recipe_hash` 标签）：底座 digest + 渲染出来的 Dockerfile +
  快照树哈希 + 配方本身。相等就整个跳过，连构建上下文都不打包。
- **docker 自己的层缓存**：`--force` 绕过上面那层，但层缓存还在。

实测（Golden 的 auth 环境）：首次 10.2 秒；再跑一次 `已是最新，跳过`；
`--force` 1.2 秒、12 步里 11 步命中缓存，**产出的 image id 和首次完全一致**。

**标签里绝对不能放构建时间。** label 是镜像配置的一部分，带时间戳的话每次构建都产生
新的 image id，"重复构建命中缓存"这条验收标准就再也观察不到了。构建时间写进
`environment_specs.built_at` 和构建证据，不写进镜像。

`--skip-base` 时也要去查一次底座的 digest，不能就这么留空：留空的话配方哈希用的是一个
固定的 tag 字符串，"底座变了"对缓存完全不可见，env 镜像会安静地停在旧底座上。

### (4) 建完必须自查，验不过这次构建就算失败

这是整个 E2-T3 里最要紧的一条，起因是一个规划文档里没写的问题：

**env 镜像里躺着一份仓库快照（`/opt/repo`，装依赖要读它的 `pyproject.toml`），
而评测时挂进来的工作区（`/workspace`）是另一个 commit、还被被测 AI 改过。**
要是 `import sqlfluff` 解析到了镜像里那一份，被测 AI 的改动根本不会被执行 ——
测试照跑、可能还全绿，而 Oracle 哨兵会从 100% 悄悄掉下去，日志里一点异常都没有。

平铺布局（包目录直接在仓库根）碰巧没事：pytest 把 rootdir 放进 `sys.path` 最前面。
src 布局就会中招 —— `/workspace` 底下压根没有 `sqlfluff` 这个名字，它在 `src/sqlfluff`。
定档的仓库里 sqlfluff 和 click 都是 src 布局。

处理方式是三步：

1. 照常 `pip install -e /opt/repo`，拿到依赖、版本号、entry_points；
2. 写一个 `.pth`（`zzz-bench-workspace.pth`），把配方声明的 `workspace_source_roots`
   插到 `sys.path` 最前面。`zzz-` 前缀是必要的：`site` 按文件名字典序处理 `.pth`，
   排在 pip 的 editable 安装那几个后面才轮得到我们插；
3. **建完当场验**：把快照挂成 `/workspace`，跑 `bench-import-check --root /workspace <包名>`，
   解析结果不在 `/workspace` 底下就让这次构建失败。

第 3 步不能省，因为第 2 步不保证成功：pip 的 editable 安装有两种实现，新的那种注册的是
meta path finder，优先级高于 `sys.path`，`.pth` 插不进去。用哪一种取决于 setuptools 版本
和仓库布局，猜不准。**所以不猜，验一次。** 这和 E2-T1「物化完自查树哈希」是同一个套路：
宁可建镜像时报错，也不要跑完 300 次评测才发现补丁根本没生效。

同一次自查顺手还跑一遍 `pytest --collect-only`（收不到用例就是 §8.8 坑 ⑥ 的症状）
并记下装完之后 pytest 的实际版本（仓库的测试依赖有可能把 base 层钉的 9.1.1 降下去，
而报告解析器的 fixture 是按 9.1.1 录的）。

**收集要按仓库自己的口径问，不能按我们编的口径问。** 配方的 `test_args` 就是干这个的 ——
照抄仓库自己的测试命令。这一条是被 LLaMA-Factory 教的：从工作区根收全部会扫到
`scripts/api_example/` 底下两个叫 `test_*.py` 的 API 用法示例（import 一个没装的 `openai`
就报错），还会撞上两个同名的 `test_converter.py`。348 条 + 3 个错，看起来像环境坏了；
换成仓库自己的 `pytest --import-mode=importlib tests/ tests_v1/` 是 **359 条 + 0 个错**。
环境一直是好的，是问法不对。

`test_args` 之后会变成 `environment_specs.test_command` 的一部分（E8-T2），
现在先在自查里用上，等于建镜像时就把它验过一遍。

自查容器**断网**（`--network none`）：评测的测试阶段就是断网的（协议 C-31、C-35），
自查也断网才能证明这个镜像离线可用，装漏的依赖会在这里现形而不是在第一道题上。

`--no-smoke` 存在，但只该在调试时用；`tests/sandbox/test_images_docker.py` 里有一条用例
专门钉死"跳过自查确实会放行一个坏镜像"，让这个开关的代价有据可查。

### (5) HOME 和 TMPDIR 放进第一层，不靠每次传环境变量

§8.8 坑 ⑤（踩过两次）的根治位置就在 bench-base：`ENV HOME=/home/bench TMPDIR=/var/tmp/bench`，
两个目录都在镜像的可写层上、权限 0777。

为什么不能靠调用方传：评测容器跟着宿主机 uid 跑，那个 uid 在 `/etc/passwd` 里没有条目，
docker 于是把 `HOME` 设成 `/`（不可写）；写进镜像，上面每一层、每一个调用点都不用再操心。
现有的两份 Agent Dockerfile 写的是 `HOME=/tmp`，那是 tmpfs、吃内存额度 ——
它们只写点配置所以没炸，但 env 镜像是给真仓库用的，pip 一退回 user 安装就会
`No space left on device`。

### (6) dockerd 会把代理注进每个构建步骤（实测）

§10.6 记的是「`docker run` 命令行会注入 `~/.docker/config.json` 里的 proxies，SDK 不会」。
**构建这一侧不一样**：2026-09-08 实测，走 SDK 触发的构建里
`RUN env | grep -i proxy` 照样打得出 `HTTP_PROXY` —— 那是 daemon 自己注进去的。

所以 `images/claude-code/Dockerfile` 里那一串 `env -u http_proxy ...` 不是多余的
（走代理拉 `deb.debian.org` 是 9.2 秒一个请求、直连 1.4 秒），bench-base 和渲染出来的
env Dockerfile 的 apt 步骤都照抄了这个写法。pip 走清华源，实测走不走代理都够快，没有绕开。

### (7) 快照来源分两个目录，浅克隆不许混进 `var/mirrors/`

建 env 镜像只要**一个** commit 的文件树，`--depth 1` 就够；而题目验证要按各题的
`base_commit` 物化工作区，需要完整历史。

这个区分是被网络逼出来的：`git clone --mirror milvus-io/pymilvus` 过代理跑了
**4 分 32 秒之后 `Connection reset by peer`**（和 E8-T1 描述的现象一致）；
换成 `git clone --bare --depth 1 --single-branch` **一次就成，2.3 MB、几秒钟**。

浅克隆放 `var/build-snapshots/`，不放 `var/mirrors/`：混进去的话
`MirrorManager.exists()` 会返回真，后面的代码以为历史是全的，然后在某个具体题目上
莫名其妙地找不到 commit。两个目录分开，这个歧义就不存在。

### (8) 磁盘水位拦在开建之前

`shutil.disk_usage(docker info 报的 DockerRootDir)`，剩余比例低于
`IMAGE_DISK_MIN_FREE_RATIO`（默认 0.15）就拒绝开建。

为什么拦在前面：docker 把磁盘写满之后倒霉的不只是这次构建 —— daemon 自己开始报错，
正在跑的评测容器跟着崩，而那时候的错误信息（某个 pytest 输出里的
"no space left on device"）根本指不到真正的原因。同一个函数 `scripts/check_env.py`
也用了一份，E9-T3 的「磁盘水位」直接复用。

### (9) 回收：判据是"有没有 tag"，不是 `bench.layer` 标签

`bench images gc` 删两类镜像：**环境已经不存在的**（配方和 `environment_specs`
里都没有了），和**被新版本顶掉、已经没有 tag 的旧构建**。

两条护栏：

- **`environment_specs.image_digest` 整列都不许删**（不只是活环境那几行）。
  协议 C-36 要求运行记录按 digest 引用镜像，删掉一个还被记着的 digest，
  等于把那次实验的可复现性抹掉，而且不会有任何报错。
- **手工建的镜像碰不到。** `bench-golden:py311`、`bench-agent:py311-aider` 没有
  `bench.owner` 标签，`gc` 的镜像列表压根不包含它们。

两处实现时才发现的事：

**① `bench.layer` 判不了"这是不是第一层"。** docker 的 label 会被子镜像继承：
建 env 时产生的中间层顶着从 bench-base 继承来的 `bench.layer=base`，
按它判断就会把这些中间层当成第一层一律保留，`gc` 于是永远收敛不掉
（实测剩 3 个、0.65 GiB）。改成看 **tag**：tag 是我们自己打上去的，不会被继承。
`bench-base:py311` 靠"有 tag 但没有环境 id"这一条保住。

**② 一轮删不干净，要循环。** 每次构建留下一条中间层的链，删掉最外面那个会让上一层
**变成**新的悬空镜像。所以 `gc` 循环到没有新候选为止（上限 20 轮）。
实测一次 `gc --yes` 删掉 74 个镜像条目，之后再跑就是"没有可以回收的镜像"。

被删掉的都是旧构建分叉之后的层，和现役镜像共用的层由 docker 自己按引用计数保住 ——
所以回收不会让下次重建从零开始，只有"改回上一版配方"才会重新付一次构建代价。

**③ 算镜像大小不能用 `images.list()` 里那个 `Size`。** 在这台机器的 containerd 镜像
存储下，那个字段报的是内容仓库里**压缩后**的大小，而磁盘上躺着的是解包后的快照，
实测差三到四倍：

| 镜像 | `images.list()` 的 Size | `/system/df` 的 Size | `docker images` 显示 |
|:---|---:|---:|---:|
| `bench-base:py311` | 0.20 GiB | 0.78 GiB | 839 MB |
| `bench-env:hiyouga__LLaMA-Factory__py311` | 3.42 GiB | 10.51 GiB | 11.3 GB |

用错的后果不是"数字不好看"：`gc` 会说自己只能回收 15 GiB 而实际是 50 GiB，
磁盘水位那套账也跟着错，而**账错的方向恰好是"看起来还很宽裕"**。
已改成走 `/system/df`（`image_disk_usage()`），它报的和 `docker images` 一致。

`gc` 报的是 **`Size - SharedSize`**，也就是"删掉它真能腾出多少"，不是"这个镜像总共多大"：
装了 torch 那个总大小 10.51 GiB、独占 9.73 GiB，底座 0.78 GiB 是和另外七个环境共用的，
删它一个腾不出 10.51。同理，`list` 底部那行把各镜像的「独占」相加，
**不等于**这批镜像占的总磁盘 —— 共用的层一次都没算进去，要总数得看 `docker system df`。

这一条是被真实磁盘教的：一轮工作下来 WSL 里从 45 GB 涨到 58 GB，而当时的工具报的
只有其中三分之一。另外 **WSL 的虚拟磁盘文件（`ext4.vhdx`）只增不减** ——
`gc` 删掉的空间在 WSL 里看是回来了，宿主机上那个文件一点没缩，
要真还回去得 `wsl --shutdown` 之后压缩它。跑重依赖仓库之前值得先看一眼余量。

### (10) 构建制品的 key 比 §17.2 多一级时间戳

`envs/{environment_id}/builds/{stamp}/` 下放 `build.log`、`requirements.lock`、`build.json`。
§17.2 那张表写的是 `envs/{environment_id}/build.log.gz`，覆盖式的。

改成带时间戳是因为：调环境镜像时最常做的事就是对比"上次能装、这次装不上"的两份日志和
两份依赖锁，覆盖掉就没得比了。`environment_specs.build_log_uri` 指向最新那一次。

**自查失败时也要落制品。** 那正是最需要看构建日志的时候（"到底装了什么，才让 import
落在了快照上"），所以 `SmokeCheckError` 带着这次构建的 `BuildOutcome` 一起抛出来。
第一版没这么做，tortoise-orm 那次失败把日志全丢了。

### (11) 实测数字（本机，2026-09-08）

| 镜像 | 首次构建 | 命中缓存重建 | 镜像大小 |
|:---|---:|---:|---:|
| `bench-base:py311` | 48.9 s | 21.3 s（15 步 4 步命中） | 831 MB |
| Golden 四个环境 | 各 9–10 s | 跳过 / 1.2 s | +0.1 MB |
| `pallets/click` | 12.7 s | — | 834 MB |
| `sqlfluff/sqlfluff` | 53.3 s | — | 1.02 GB |
| `tortoise/tortoise-orm` | 43.3 s | — | 1.05 GB |
| `hiyouga/LLaMA-Factory` | **10 分 26 秒** | 17.7 s（13 步 11 步命中） | 3.42 GB |

env 镜像共享 base 层，表里的"大小"是 `docker images` 报的总量，
**不是**每个环境额外占的磁盘。8 个环境 + 1 个 Agent 层实际增量约 4.5 GB，
其中 LLaMA-Factory 一个就占 3.2 GB（torch 那一套）。按这个比例，
剩下三个大型国产项目建完大约再加 10 GB，离 §10.4 说的 80 GB 上限仍然很远。

**大仓库的一次性成本是真的大**：LLaMA-Factory 从零建要 10 分半。但这正是 ADR-008 说的
"可在实验前夜完成"—— 它和评测时的单题耗时无关，改配方之后重建只要 17.7 秒，
装依赖那一层原样复用。
