# 使用文档

> 一句话：平台的日常操作都是命令行（`python -m cli.<模块> <子命令>`），前端和 HTTP 接口用来看结果。
> 完整旅程是八步：建镜像 → 灌题 → 配 Agent → 建实验 → 看进度 → 排行榜 / 报告 → 归因 / 抽检 → 发布数据集。

**这份文档给谁看**：平台已经按 [`deployment.md`](deployment.md) 装好了，现在要用它给 AI 编程助手打分的人。
每条命令的参数都对照过 `--help`（2026-09-21，main = `899aed0`）。命令的原理和取舍见 [`architecture.md`](architecture.md)。

---

## 0. 开始之前

### 0.1 命令怎么敲

平台有两种跑法，命令本体一样，前缀不同：

| 跑法 | 命令怎么写 | 说明 |
|:---|:---|:---|
| **compose 部署**（`deployment.md` 装出来的） | `make compose-cli CMD="python -m cli.experiment status"` | 在一个临时容器里跑，不用装 uv |
| **开发模式**（`make dev` + `make db-up` + `make worker`） | `cd backend && uv run python -m cli.experiment status` | 直接在宿主机跑 |

下文的命令一律只写 `python -m cli.…` 这一段，你按自己的跑法加前缀。两种跑法的工作目录都是 `backend/`，
所以引用仓库里别的文件要写 `../datasets/…`。

`Makefile` 里的快捷目标（`make seed`、`make dataset-stage` 之类）大多是 `cd backend && uv run …` 的包装，**只在开发模式可用**；
少数是纯 `docker build` 的（`make images` / `make images-aider` / `make images-claude-code`），两种跑法都能用。
`make help` 列出全部目标。

### 0.2 几个词

| 词 | 意思 | 在库里 |
|:---|:---|:---|
| **题**（task） | 一个真实开源项目的 bug：代码回退到修复前的快照 + 当时的 issue + 两组测试（`fail_to_pass` 修好后必须由挂变过，`pass_to_pass` 不能被改坏） | `benchmark_tasks` |
| **数据集版本**（benchmark set） | 一批题冻成的快照，如 `benchmark-cn-v1@v2`。实验从快照里取题，不从题表里取，所以题表后来改了也不影响已跑的实验 | `benchmark_sets` + `benchmark_set_items` |
| **Agent** 与 **配置** | Agent 是适配器（aider、claude-code、自研 MiniAgent…）；配置 = Agent × 模型 × 参数，**排行榜上的选手是配置**，如 `aider@deepseek-flash` | `agents` / `agent_configs` |
| **实验**（run） | 一个配置 × 一个数据集版本跑一遍。`#158` 这种编号就是它 | `evaluation_runs` |
| **单次执行**（task run） | 一道题的一次尝试，平台故障会重试，一道题可能有多条；被采信的那条标 `is_canonical` | `evaluation_task_runs` |
| **哨兵** | 三个不调大模型的 Agent：**Oracle** 交官方补丁（解决率必须 100%）、**Noop** 交空补丁（必须 0%）、**Mock** 行为可编程。它们是量具不是选手，不进排行榜 | `agents.kind` |

### 0.3 三条纪律

1. **建实验时仓库工作区必须干净**（`git status --porcelain` 为空，协议 C-27）。改了文件没提交就建实验会被拒；`--allow-dirty` 放行，但结果标 `dirty=true`，不进排行榜。
2. **一台机器只跑一个 Worker。** compose 里已经有一个在跑，别再 `make worker`（`deployment.md` §4）。
3. **开发模式下，跑任何一个集成测试会把它连上的库清空。** `make test` / `make check` 已经指到独立的 `bench_test` 库；绕开 Makefile 直接 `uv run pytest` 就会连回开发库（`AGENTS.md` 第 9 节）。compose 部署不受影响 —— 它不跑测试。

---

## 1. 全景：八步各用什么命令

```
① 建镜像      python -m cli.images build …  / make images / make images-aider
② 灌题        cli.golden → cli.queue seed-golden → cli.validate run       （自带四题）
              cli.mine → cli.prescreen → cli.promote → cli.validate run    （GitHub 挖掘）
              cli.swebench fetch → sample → mirror → build → import → cli.validate run （官方题）
③ 配 Agent    python -m cli.seed   +  .env 里的 API Key  +  Agent 镜像
④ 建实验      python -m cli.experiment start --agent … --config … --set …
⑤ 看进度      python -m cli.experiment status [--run N]   /  GET /api/runs/{id}
⑥ 排行榜/报告  GET /api/leaderboard?set=…   /  python -m cli.report generate --run …
⑦ 归因/抽检    python -m cli.attribute rules | llm   /  前端 /review
⑧ 发布数据集   python -m cli.dataset stage → gate（要 Worker）→ publish
```

第一次用，最快的一条路是 `deployment.md` 第 6 步的 `make compose-smoke`：它把 ①②④⑤ 用四道 Golden 题串了一遍。
下面按八步分别讲。

---

## 2. 建镜像

评测在容器里跑，先要有镜像。一共四种，前三种是分层的（`docs/plan/05-sandbox.md` §10.4，ADR-008）：

| 镜像 | 干什么 | 怎么建 | 体积（`docker images` 显示） |
|:---|:---|:---|---:|
| `bench-base:py311` | 第一层：Python 3.11 + git + pytest，所有环境镜像的底座；也是自研 MiniAgent 的运行镜像 | `python -m cli.images build --base-only` | 876 MB |
| `bench-env:<环境 id>` | 第二层：某个开源仓库的依赖装好了，测试阶段在它里面跑。**一个仓库一个**，配方在 `images/envs/<环境 id>.json` | `python -m cli.images build --env pallets__click__py311`（`--env` 可重复；不给就建全部配方） | 小仓库几百 MB 且几乎全是和底座共享的层；装了深度学习依赖的 10 GB |
| `bench-agent:py311-aider` / `py311-claude-code` | 第三层：装了被测 AI 的命令行工具。Agent 阶段在它里面跑 | `make images-aider` / `make images-claude-code`（叠在 `bench-golden` 上，要先 `make images`） | 1.23 GB / 1.07 GB |
| `bench-golden:py311` | 手写的小镜像，只给四道 Golden 题的测试阶段用 | `make images` | 232 MB |

要点：

- **第二层建完会把镜像 digest 写回数据库**（`environment_specs.image_digest`，协议 C-36 要求实验按 digest 引用镜像，不按 tag）。
  但它**只更新已有的行、不建行** —— 所以顺序是先把题组装入库（§3），再 `cli.images build --env …`，反过来 digest 永远是空的。`--no-db` 是"只建镜像不连库"，调试用。
- 配方没变就跳过（按配方哈希比对），`--force` 强制重建，`--no-cache` 连 docker 层缓存也不用。建完自带一次自查（能 import、能收集到测试），`--no-smoke` 跳过 —— **只在调试时用**，自查挡的是"依赖没装对但不报错"这类故障。
- 磁盘：分区余量低于 15% 拒绝开建。`python -m cli.images list` 看现有镜像和水位；`python -m cli.images gc` 列出可回收的（默认只列，`--yes` 才删）。
- **大镜像建之前先 `docker images` 看一眼在不在**：`xorbitsai__inference__py311` 10.4 GB、建一次十几分钟。

**镜像源的坑（2026-09-21）**：`images/base/Dockerfile` 和 `images/envs/*.json` 默认用清华源，当天清华的 debian 和 pypi 都返回 403。撞上时：

- 第一层：改 `images/base/Dockerfile` 里 `ARG APT_MIRROR=…` 和 `ARG PIP_INDEX_URL=…` 两行的默认值（比如换成 `https://mirrors.aliyun.com/debian` 和 `https://mirrors.aliyun.com/pypi/simple`）。改了 Dockerfile 配方哈希会变，会重建，这是预期的。
- 第二层：在对应的 `images/envs/<环境 id>.json` 里加 `"pip_index_url"` / `"apt_mirror"` 两个字段（配方认这两个键，写错键名会直接报错而不是忽略）。
- 第三层：`docker build --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple -t bench-agent:py311-aider images/aider`（claude-code 那份是 `APT_MIRROR` 和 `NPM_REGISTRY`）。

---

## 3. 灌题

题有三个来源。**第一次用先走 3.1**，五分钟内能看到结果；3.2 和 3.3 是正式题库的来路，各要几小时到一天。

### 3.1 自带的四道 Golden 题

仓库里 `datasets/golden/` 有四道人工写的小题（auth / cart / pager / textkit），是整条链路的测试基石：

```bash
python -m cli.golden build                 # 从 sources/ 生成任务 JSON 和本地 git 镜像（幂等）
python -m cli.queue seed-golden            # 题目入库（开发用，不跑验证）
python -m cli.validate run --task bench-golden__auth-2 --task bench-golden__cart-3 --task bench-golden__pager-4 --task bench-golden__textkit-1
python -m cli.dataset stage --dataset-id golden-v1 --slug golden   # 冻一版草稿快照，实验才能从里面取题
```

`make compose-smoke` 做的就是这四步再加两个实验。`cli.golden verify` 是另一条独立的六步自检，`cli.golden list` 列题。

### 3.2 自建题：从 GitHub 挖

流水线是 **挖掘 → 清洗 → 预筛（大模型打分，花钱）→ 探测（起容器证伪）→ 组装成题 → 建环境镜像 → 八步验证 → 人工终审**。每一步一个子命令：

```bash
python -m cli.mine run --repo pallets/click                # 挖 merged PR + 关联 issue → task_candidates（要 GITHUB_TOKEN）
python -m cli.prescreen clean                              # 脱敏 + 拆补丁 + 抽候选 F2P（不花钱）
python -m cli.prescreen score --model deepseek/deepseek-chat --limit 20   # 大模型打分（**花钱**，先 --limit 小批量试）
python -m cli.promote probe                                # 起容器实测：F2P 修前真的挂、修后真的过，顺带派生 P2P（要 Docker）
python -m cli.promote assemble                             # 探测通过的候选 → benchmark_tasks
python -m cli.images build --env pallets__click__py311     # 这时才建环境镜像，digest 写回题目的环境规格（§2 说的顺序）
python -m cli.validate run --dataset benchmark-dev         # 八步验证，结论写回 validation_state（VALID / INVALID / REVIEW_REQUIRED）
python -m cli.promote export-review                        # 导出人工终审对照表 CSV（默认 datasets/benchmark-dev/review-<日期>.csv）
python -m cli.promote import-review ../datasets/benchmark-dev/review-2026-09-10-final.csv --reviewer 你的名字   # 填好 verdict 列导回
python -m cli.promote park-unreviewed ../datasets/benchmark-dev/review-2026-09-10-final.csv   # 没人审过的退回 REVIEW_REQUIRED
python -m cli.promote report                               # 漏斗：每一层剩多少、掉队的为什么
```

几件要知道的事：

- **人工终审的结论不在库里，在提交进仓库的 CSV 里**（`datasets/benchmark-dev/review-*.csv`）。清了库重灌，要把四份 CSV 都 `import-review` 回去，再 `park-unreviewed`，否则没审过的题会混进快照。完整重灌顺序在 `AGENTS.md` 第 12 节，**顺序不能换**。
- 挖掘和预筛的结果有本地文件缓存（`var/cache/`）。接手别人的环境时一定要拿到这个目录（`CONTRIBUTING.md` §1.5 的交接包），有它重跑是纯走缓存、不花钱不占 GitHub 配额。
- 现在库里的自建题来自 `pallets/click` 和 `tortoise/tortoise-orm`（65 道入库、41 道 VALID）。中文题面是另一步：`python -m cli.localize import ../datasets/benchmark-dev/localize-2026-09-18.csv` 把复核过的中文题面导回（只换题面，测试和补丁不动）。
- 这条流水线是**有意单向**的：候选推成题之后状态是 `PROMOTED`，`assemble` 只挑 `PRESCREENED` 的。只删 `benchmark_tasks` 想重来是不行的，`AGENTS.md` 第 12 节末尾有两条 SQL。

### 3.3 官方题：SWE-bench Verified 子集

用来校准（和学术界的基准对得上），slug 单独是 `swebench-verified-subset`，**不进自建题的统计**：

```bash
python -m cli.swebench fetch                               # 官方数据集 500 行 → var/cache/swebench/（有缓存不重拉）
python -m cli.swebench sample --seed 20260915 --n 100      # 固定种子分层抽样，名单写进 datasets/swebench/（重算必须逐字相同）
python -m cli.swebench mirror --seed 20260915 --n 100      # 按 base_commit 浅拉 git 镜像
python -m cli.swebench build --seed 20260915 --n 100 --jobs 2     # 按官方配方本机建镜像（官方镜像在这台机器上拉不动）
python -m cli.swebench import --seed 20260915 --n 100      # 题目 + 环境规格入库（只收镜像已建好的）
python -m cli.validate run --dataset swebench-verified-subset --scope declared   # 只跑声明的用例，不跑全量
python -m cli.swebench review-csv                          # 停在 REVIEW_REQUIRED 的 → 终审对照表；填完 cli.promote import-review 导回
python -m cli.swebench report --seed 20260915 --n 100 --save   # 导入漏斗
```

`--scope declared` 是关键区别：官方题的 P2P 已经给定，跑全量套件一道题要一小时。
镜像那一步很重：抽 100 题去重后几十个镜像、几十 GB，matplotlib 的 conda 环境每个吃 5 GB 内存，只能 `--jobs 1`。
建之前 `docker images | grep -c bench-env:swebench` 看一眼有多少已经在了。细账在 `AGENTS.md` 第 12 节"E1-T7 之后多出来的那一段"和 `docs/plan/03-benchmark-spec.md` §8.6。

### 3.4 看题库

```bash
python -m cli.validate show                                # 每道题的验证状态；--task 只看几道
python -m cli.dataset show                                 # 有哪些数据集版本；--tasks 连题目清单一起打印
curl -s "http://localhost:8000/api/tasks?state=VALID&repo=pallets/click"   # HTTP 接口；?set= 收数据集版本的数字 id，从 /api/benchmark-sets 查
```

开发库里现在的版本（`cli.dataset show`，2026-09-21）：

```
版本                         状态            题数  来源 dataset_id    摘要
benchmark-cn-v1@v1         PUBLISHED     41  benchmark-dev    sha256:1701c943ff5b…
benchmark-cn-v1@v2         PUBLISHED     41  benchmark-dev    sha256:19a2508ae0e1…
benchmark-dev@v1           PUBLISHED     22  benchmark-dev    sha256:300746559b84…
golden@v1                  DRAFT          4  golden-v1        sha256:7fde023f6dca…
swebench-verified-subset@v1 PUBLISHED     42  swebench-verified-subset sha256:270b811da38a…
swebench-verified-subset@v2 PUBLISHED     59  swebench-verified-subset sha256:4f5b44b88b46…
swebench-verified-subset@v3 PUBLISHED     75  swebench-verified-subset sha256:6003a0519a52…
```

最终实验用的是 `benchmark-cn-v1@v2`（41 道自建题，中文题面）和 `swebench-verified-subset@v3`（75 道官方题）。

---

## 4. 配 Agent

### 4.1 种子：一条命令写入全部 Agent 和配置

```bash
python -m cli.seed
```

它**不接参数**（`--help` 也会被当成"直接跑"），可重复执行：Agent 按 `name`、配置按 `label` 查重，已有的只更新展示字段、单价和参数。
**改了 `backend/cli/seed.py` 之后必须重跑一次**，库里的单价和参数才会跟上。现在写进去的：

| Agent（`--agent`） | 配置标签（`--config`） | 底座模型 | 用途 |
|:---|:---|:---|:---|
| `oracle` | `oracle@gold` | 不调模型 | 哨兵：交官方补丁，必须 100% |
| `noop` | `noop@empty` | 不调模型 | 哨兵：交空补丁，必须 0% |
| `mock` | `mock@programmable` | 不调模型 | 哨兵：六种行为可编程，测失败路径 |
| `miniagent` | `miniagent@deepseek-flash` | deepseek-flash | **自研** Agent，四工具 ReAct，跑在 `bench-base` 里 |
| `aider` | `aider@deepseek-chat`（旧，留给 pilot 历史）/ **`aider@deepseek-flash`** | deepseek-flash（关思考） | 真实 CLI Agent，镜像 `bench-agent:py311-aider` |
| `claude-code` | `claude-code@deepseek-chat`（旧）/ **`claude-code@deepseek-flash`** | deepseek-flash，走 DeepSeek 的 Anthropic 兼容端点 | 真实 CLI Agent，镜像 `bench-agent:py311-claude-code` |

`aider` 和 `claude-code` 各有两份启用配置，**建实验时 `--agent` 不唯一，必须再给 `--config`**（不给会报错，不会猜）。
三个真实选手底座全是 deepseek-flash，比的就是 Agent 框架本身。`deepseek-chat` 现在只是 flash 的别名（2026-09-20 实测），别拿旧配置跑新实验。

### 4.2 密钥和代理

在 `.env` 里填，容器启动时按**白名单**注入 Agent 容器（`app/sandbox/container.py` 的 `AGENT_ENV_ALLOWLIST`：四家的 Key、几个 `*_BASE_URL`、代理三件套；名字不在名单里的变量不会进容器）：

| 项 | 给谁 | 备注 |
|:---|:---|:---|
| `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `DASHSCOPE_API_KEY` | 被测 AI | 用哪家填哪家。**DeepSeek 余额掉到 0 的表现是全部评测记 `AGENT_AUTH_ERROR`**（平台故障，不算 AI 的），跑大批量前先看余额 |
| `SANDBOX_HTTP_PROXY` | Agent 容器出网 | 国产模型直连留空；打境外模型时填 WSL 网关地址 |
| `JUDGE_MODEL` / `JUDGE_API_KEY` / `LLM_HTTP_PROXY` | **平台自己**调模型的两处：候选预筛（§3.2）、失败归因（§8） | 和被测 AI 的 Key 分开；`LLM_HTTP_PROXY` 留空 = 直连 |

测试阶段的容器**始终断网**，这些都碰不到它。

### 4.3 Agent 镜像

见 §2：`make images && make images-aider && make images-claude-code`。MiniAgent 跑在 `bench-base:py311` 里，不用单独建。
镜像不在本地时评测直接报错，**不会自动拉**。

### 4.4 加一个新配置 / 新 Agent

- **同一个 Agent 换模型或参数**：在 `backend/cli/seed.py` 的 `SEED_AGENTS` 里加一个 `SeedAgent`（`name` 不变、`config_label` 新起、填单价），重跑 `cli.seed`。价目写进配置是为了成本估算 —— Agent 报不出费用时按 token × 单价估，报告里标 `estimated`。
- **接一个全新的 Agent**：实现 `app/runner/adapters/` 里的 `AgentRunner`（`probe()` / `run()`），继承 `tests/contract/runner_contract.py` 的契约测试套件，再加种子。协议是冻结件（`docs/plan/04-runner-protocol.md`）：标准输入一行 JSON 任务，标准输出**最后一行**一个 JSON 结果，带 unified diff。
  "这次失败该记在谁头上"不要自己判，调 `adapters/cli_text.shared_failure()`（`AGENTS.md` §5.4）。

### 4.5 确认配好了

```bash
curl -s http://localhost:8000/api/agents
curl -s http://localhost:8000/api/agent-configs
docker image inspect bench-agent:py311-aider --format '{{.Id}}'
```

然后**先拿一道题试**（下一节的 `--task`），别一上来跑整份数据集。

---

## 5. 建实验

前提三条：数据集版本已经存在（发布过的，或 Golden 那版草稿）；Worker 在跑；工作区干净。

```bash
python -m cli.experiment start --agent oracle --set golden --name 试一下                          # 哨兵，不花钱
python -m cli.experiment start --agent aider --config aider@deepseek-flash --set benchmark-cn-v1 --task pallets__click-2271 --name 单题试跑   # 先试一题
python -m cli.experiment start --agent aider --config aider@deepseek-flash --set benchmark-cn-v1 --name "aider 第 1 轮"            # 整份数据集
python -m cli.experiment start --agent miniagent --set swebench-verified-subset --rounds 2 --name "miniagent 两轮"                # 两轮 = 两个独立实验
```

参数（`cli.experiment start --help`）：

| 参数 | 意思 |
|:---|:---|
| `--agent` | Agent 名字，默认 `oracle` |
| `--config` | 配置标签。该 Agent 有多份启用配置时**必须给** |
| `--set` / `--version` | 数据集 slug 和版本，版本不给取**最新已发布**的那一版（Golden 没发布过，会提示用的是草稿） |
| `--task` | 只投这几道题（`task_id`，可重复）。只跑部分题的实验**不进排行榜**（分母不对） |
| `--rounds` | 跑几轮。每一轮是一个独立的实验编号（协议 C-55），轮间解决率的差就是随机性的大小 |
| `--agent-concurrency` / `--sandbox-concurrency` | 记进实验的并发数，默认取 `.env`（`AGENT_CONCURRENCY=10` / `SANDBOX_CONCURRENCY=4`） |
| `--allow-dirty` | 工作区不干净也建，结果标 `dirty=true`，不进排行榜 |

回显长这样（来自 compose 冒烟）：

```
实验 #1（compose-smoke-oracle）已建，投了 4 条 EVAL_TASK 作业
起 Worker 来跑：python -m app.worker
看进度 / 取消 / 补跑：python -m cli.experiment status --run 1
```

它只是**把题投进作业队列**，真正跑的是 Worker（compose 里的 `worker` 服务）。`make enqueue AGENT=aider CONFIG=aider@deepseek-flash SLUG=benchmark-cn-v1 NAME=xxx` 是开发模式的等价快捷方式（走的是 `cli.queue enqueue`，参数少一些）。

HTTP 也能建：`POST /api/runs`，请求体 `{"benchmark_set_id": …, "agent_config_id": …, "name": "…", "rounds": 1}`，头 `X-Bench-Token: <你的 ADMIN_TOKEN>`。

**费用**：真实 Agent 每题几分钱到几毛钱（aider 在大仓库上几乎不命中缓存、每题约 ¥0.4，最贵）。平台按美元牌价估的成本比 DeepSeek 的人民币账单高约 1.3 倍。

---

## 6. 看进度

```bash
python -m cli.experiment status                 # 最近的实验列表；--limit N
python -m cli.experiment status --run 158       # 一个实验的细账
python -m cli.queue status                      # 作业队列：各状态各多少条
```

列表回显（开发库，2026-09-21）：

```
   #  Agent      状态                进度      解决    故障         成本  名字
 170  aider      COMPLETED      75/75   19/75     3     0.8955  E10-T4 第 2 轮 · aider · swebench-verified-subset@v3
 169  aider      COMPLETED      41/41    6/41     0     0.4001  E10-T4 第 2 轮 · aider · benchmark-cn-v1@v2
 168  claude-code COMPLETED      75/75   68/75     0     1.5791  E10-T4 第 2 轮 · claude-code · swebench-verified-subset@v3
```

"故障"那一列是**平台故障**数（沙箱出错、被 OOM 杀掉、模型接口鉴权失败…），和"AI 没修好"分开记 —— 这是协议最核心的一条：
一次评测的结果用三个互相独立的字段描述（走到哪一步 / 平台有没有正确完成 / AI 有没有修好），把两种失败混在一起解决率就不可信。
平台故障率超过 5% 的实验状态记 `PARTIAL`，不进排行榜（C-26）。

单个实验的细账里要看的：严格解决率（分母 = 全部题数，排行榜用它）、有效解决率（分母 = 可归因于 AI 的题数，只用于自查）、
重试次数和"救回来"几次、成本来源分布（`reported` 是 Agent 自报的，`estimated` 是按 token 估的，`unavailable` 是报不出来）。

HTTP：

```bash
curl -s http://localhost:8000/api/runs                                  # 列表；?status= 过滤
curl -s http://localhost:8000/api/runs/158                              # 一个实验
curl -s "http://localhost:8000/api/runs/158/task-runs?status=COMPLETED" # 逐题
curl -s http://localhost:8000/api/task-runs/4321                        # 一次执行
curl -s http://localhost:8000/api/task-runs/4321/tests                  # 逐条用例结果（F2P / P2P 各自过没过）
curl -s http://localhost:8000/api/task-runs/4321/artifacts/AGENT_STDOUT # 制品原文；kind 还有 AGENT_STDERR / TEST_STDOUT / TEST_REPORT_XML / TRAJECTORY / AGENT_RAW / AGENT_NORMALIZED
```

"某个 Agent 在某道题上为什么失败"的完整证据 = 最后三条：AI 的补丁（`AGENT_NORMALIZED` 是过滤掉测试文件改动之后真正打上去的那份，`AGENT_RAW` 是它原始交的）、逐条用例、日志和轨迹。
`/api/task-runs/{id}` 的 JSON 里还带 `failure_attribution`（§8.1 的自动归因：类别、哪一层判的、置信度、证据、中文理由；没有结论是 `null`），
前端单题页的"失败归因"区块就是它。盲检开关开着时它一律是 `null` 且 `attribution_withheld=true`，见 §8.2。

Worker 日志：`make compose-logs SERVICE=worker`（开发模式看 `make worker` 的终端）。两条要认识的告警：
`container_sigkilled_without_oom_flag`（容器被 137 杀掉但没有 OOM 标志，并发下有 3–5% 概率是漏报的内存超限）和 `startup_reaped_containers`（另一个 Worker 起来了，它把别人的容器当孤儿杀了）。

**取消和补跑**：

```bash
python -m cli.experiment cancel --run 158          # 取消：正在跑的题记 CANCELLED，不算 AI 的失败
python -m cli.experiment retry-failed --run 158    # 只补"没有结论"的题（C-25）；已有结论的平台故障不会重判
```

---

## 7. 排行榜和报告

### 7.1 排行榜

一个数据集版本一张榜，不同数据集之间不比：

```bash
curl -s "http://localhost:8000/api/leaderboard?set=benchmark-cn-v1"                    # 不给 set 取最新已发布的那一版
curl -s "http://localhost:8000/api/leaderboard?set=benchmark-cn-v1&version=v2&metric=cost"       # metric: resolve_rate（默认）/ cost / duration
curl -s "http://localhost:8000/api/leaderboard?set=swebench-verified-subset&facet=repository"    # facet: difficulty / language / repository
```

响应里带 `eligibility`（准入规则原文）和 `excluded_runs`（被排除的实验和理由）。准入六条：实验状态 `COMPLETED`、`dirty=false`、没被人工排除、配置是启用的、不是哨兵、跑满了整份快照。

人工排除一次实验（比如某轮模型余额耗尽、数字不是测量结果）：

```bash
python -m cli.experiment exclude --run 157 --reason "aider 开了思考的对照组，和其他选手条件不同"
python -m cli.experiment include --run 157      # 撤销
```

理由会原样显示在榜单下面，所以要写清楚。排除是加一条注，**不改原来的判定字段**。

### 7.2 报告

```bash
python -m cli.report generate --run 158 --run 159 --run 161 --run 165 --run 167 --run 169 --title "benchmark-cn-v1@v2 两轮" --base-url http://localhost:8000
```

一条命令对一个或多个**同数据集、同协议版本**的实验生成 HTML、Markdown、JSON 三份（同一份中间结构渲染出来的），登记到 `artifacts` 和 `report_records`，
文件落在 `var/artifacts/runs/<第一个实验号>/reports/<时间戳>/`。内容：严格 / 有效解决率、轮间极差、逐题翻转率、难度 / 语言 / 仓库分面、
平台故障与重试、成本（三种来源分开，缺的不显示成 `$0`）、失败分类、Agent × 类别、Top-N 失败案例（`--top-n`，默认带补丁 / 日志 / 轨迹链接，链接的根地址用 `--base-url`）。
没做完的事（LLM 归因、盲检准确率、κ）报告里**主动写"未做"**，不留空。

两个辅助命令：`python -m cli.experiment manifest --run 158`（可复现性清单：镜像 digest、harness 的 git sha、数据集摘要、环境变量白名单；给两个 `--run` 就是比对），
`python -m cli.experiment timing --run 158`（各阶段耗时 P50 / P95、makespan 投影）。

### 7.3 前端

现在只有两页：`/`（平台自检）和 `/review`（人工盲检工作台，§8.2）。排行榜、实验列表、单次执行详情这些页面在做（E7），
做好之前用上面的 HTTP 接口看 —— `http://localhost:8000/docs` 是可以点着试的接口文档。

---

## 8. 归因和抽检

判定（修好没修好）**100% 由测试结果推导**，不用大模型。大模型只用在"分析为什么没修好"这一步，而且它的输出不回写判定结果（协议 C-40，ADR-011）。
失败分 10 类：F1～F8 是 AI 的问题（F1 理解错需求、F2 改错文件、F3 修了一半、F4 逻辑错、F5 语法 / 构建错、F6 改坏了别的（回归）、F7 空补丁或补丁打不上、F8 工具 / 预算耗尽），N1 是平台故障，N2 是题目本身有缺陷。

### 8.1 自动归因

```bash
python -m cli.attribute rules                        # 规则层：F6 回归 / F7 空补丁 / F8 超时 / N1 平台故障，确定性、不花钱；不给参数扫全库、判过的跳过
python -m cli.attribute rules --run-id 4321 --redo   # --run-id 是**单次执行**的 id，不是实验号；--redo 连判过的也重判；--dry-run 只看分布
python -m cli.attribute features --run-id 4321       # 看一次执行的结构化特征（改了哪些文件、失败用例的报错怎么变的）
python -m cli.attribute llm --limit 1 --dry-run      # 大模型归因 F1～F5：先 --dry-run 看会处理哪些、prompt 指纹
python -m cli.attribute llm --limit 1                # **花钱**。首次付费试跑建议 --limit 1；--model 缺省读 .env 的 JUDGE_MODEL
```

规则层已经跑过全库（859 条，覆盖 75% 的失败）；剩下 177 道规则分不出的要 LLM 归因或人工。**LLM 归因的代码做完了但没跑过真模型**，跑之前先看余额、先问负责人。

### 8.2 人工抽检（盲检）

打开前端 `http://localhost:3000/review`，输入自己的名字、管理员令牌（`.env` 的 `ADMIN_TOKEN`）和随机种子，
左边是分层抽出来的待标注队列（每个自动类别至少 5 条、总计 50 条，固定种子每次抽出同一批），右边三栏：题面与官方补丁摘要、AI 的补丁与轨迹、逐条用例。
选 F1～F8 / N1 / N2，提交。**提交之前看不到机器给的答案**（这就是"盲"），提交后才显示对照。两人独立标，不一致由第三人仲裁。

**抽检前先打开盲检开关。** 单题页 `/task-runs/{id}` 和它背后的 `GET /api/task-runs/{id}` 不要令牌、平时直接显示机器归因，
而队列里就写着 `task_run_id`——不藏的话标注的人在地址栏敲一下就能提前看到答案。`.env` 里设 `BENCH_BLIND_REVIEW=true`、重启 api
（开发模式重起 `make dev-api`；compose 是 `make compose-down && make compose-up`），这个接口就把 `failure_attribution` 置空、
标 `attribution_withheld=true`，页面显示"盲检进行中"。标完改回 `false` 再重启。`/review` 的三个接口不受它影响，它们自己有"提交前不返回"的规则。
报告里要写明抽检是在开关打开的状态下做的（`06-judge-attribution.md` §12.5 的要求）。

HTTP 同款：`GET /api/review/queue?reviewer=你的名字&seed=20260920&target_size=50`、`GET /api/review/{task_run_id}`、`POST /api/review/{task_run_id}`，都要 `X-Bench-Token` 头（复核详情含官方补丁摘要，不能开放读）。

准确率和 κ 的计算（E6-T4）**还没做**，标注的原始标签存在 `human_reviews` 表里，做了就能算。

---

## 9. 发布数据集

题验证过（`VALID`）不等于能用，要冻成版本、过门禁、发布，实验才能从它里面取题。三步：

```bash
python -m cli.dataset stage --dataset-id benchmark-dev --slug benchmark-cn-v1     # 冻快照：该 dataset_id 下全部 VALID 的题 → 一版 DRAFT
python -m cli.dataset gate --slug benchmark-cn-v1                                  # 建 Oracle / Noop 两个门禁实验并投队列（要 Worker 在跑）
python -m cli.dataset publish --slug benchmark-cn-v1                               # 查门禁：Oracle 100% 且 Noop 0% 才发布（协议 C-50）
```

- `--dataset-id` 是题目归属（`raw_definition->>'dataset_id'`），`--slug` 是数据集名，两者可以不同名：现在的自建题 `dataset_id` 都是 `benchmark-dev`，slug 有 `benchmark-dev` 和 `benchmark-cn-v1` 两个。
- `stage` 对同一批内容重复跑是 noop；内容变了就出下一版（v1 → v2）。已发布的版本**不动**，实验按版本取题，摘要（`snapshot_digest`）可核对。
- 门禁不过就是有坏题或判定有 bug，**不要为了过门禁改判定**（`AGENTS.md` §5.5）。剔题的做法是把会飘 / 依赖顺序的用例从 P2P 里去掉重新组装，再 stage 出新版本。
- `--version` 默认取最新一版；`gate --force` 已有门禁在跑也照投；两个命令都有 `--allow-dirty`（发布指纹里会标 dirty）。

发布后的维护：

```bash
python -m cli.dataset show --slug benchmark-cn-v1 --version v2 --tasks       # 看某一版的题目清单
python -m cli.dataset verify --slug benchmark-cn-v1                         # 拿快照比对现在的题库，报漂移（纯查询）
python -m cli.dataset quarantine --task pallets__click-2271 --reason "P2P 里 test_x 依赖执行顺序"   # 隔离一道题：下一版排除，已发布版本不动
python -m cli.quality report --save                                          # 数据集质量报告：来源 / 语言 / 难度 / 漏斗，落 datasets/quality/
```

---

## 10. 常见错误

| 报错 / 现象 | 原因 | 怎么办 |
|:---|:---|:---|
| 建实验被拒，提示工作区不干净 | 协议 C-27 | 提交或还原改动；调试时 `--allow-dirty`（不进榜） |
| `--agent aider` 报"多份配置" | 同名 Agent 有两份启用配置 | 加 `--config aider@deepseek-flash` |
| 实验建好了一直 `QUEUED` | Worker 没在跑 | compose：`make compose-ps` 看 `worker` 是否 healthy；开发模式 `make worker` |
| 一道题都选不出来 / 快照是空的 | 题没到 `VALID`，或数据集没 stage | `cli.validate show` 看状态；`cli.dataset show` 看有没有版本；人工终审的 CSV 有没有 `import-review` |
| 评测报镜像不存在 | 平台不会自动拉 / 建镜像 | §2 建好；`cli.images list` 核对 |
| 全部评测记 `AGENT_AUTH_ERROR` | 模型 Key 无效或余额为 0 | 看控制台余额；这类记平台故障，不算 AI 的 |
| `environment_specs.image_digest` 为空 | 先建镜像后组装题（顺序反了） | 重跑 `cli.images build --env …`，它只更新已有的行 |
| 解决率莫名偏低、大量 `MISSING` 用例 | 用例 ID 归一化问题（`tests/x.py::t` 和 `./tests/x.py::t` 是同一个） | 这是判定引擎的 bug，不是题的问题，报 issue（`AGENTS.md` §5.5） |
| 一次真实验解决率和上一轮差好几个点 | 大模型本身的随机性；两轮实测极差 ≤4 pp，逐题翻转 17–43% | 跑 `--rounds 2` 以上，小于 5 pp 的差别不下结论 |
