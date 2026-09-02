# 项目开发规则

> 本文件是这个项目的**唯一开发规则来源**，面向所有 AI 编程助手（Claude Code、Codex、Cursor、Aider 等）和人类开发者。
> `CLAUDE.md` 只是指向本文件的入口，不重复内容。

---

## 1. 这个项目是什么

给 AI 编程助手打分的平台。

流程是这样的：从真实开源项目里挑一个已经被修复过的 bug，把代码回退到修复之前的状态，把当时的 issue 描述交给被测的 AI，让它自己去改代码。改完之后，用项目原本的测试来验证它改对了没有。

参考 SWE-Bench 的做法（这是学术界评测 AI 编程能力的通用基准）。

**当前阶段：规划已完成，业务代码一行都还没写。** 目前仓库里只有 `docs/plan/` 下的规划文档。

---

## 2. 动手之前先读什么

不要跳过这一步。这个项目的坑大多不在代码里，而在协议定义里。

| 你要做什么 | 必读 |
|:---|:---|
| 任何工作 | `docs/plan/README.md`（摘要）· `docs/plan/12-engineering-workflow.md`（本文件的详细版） |
| 碰判定逻辑 / 数据库枚举 | `docs/plan/02-evaluation-semantics.md` ⚠️ 冻结件 |
| 碰任务格式 / 数据集 | `docs/plan/03-benchmark-spec.md` ⚠️ 冻结件 |
| 接入新的 AI 助手 | `docs/plan/04-runner-protocol.md` ⚠️ 冻结件 |
| 碰 Docker / 沙箱 | `docs/plan/05-sandbox.md` |
| 碰判定引擎 / 失败归因 | `docs/plan/06-judge-attribution.md` |
| 碰数据库 / 后端 / 前端 | `docs/plan/07-platform-architecture.md` |
| 想知道"为什么这么设计" | `docs/plan/08-adr.md`（12 条架构决策记录） |
| 领任务 | `docs/plan/10-tasks-plan.md`（任务树）· `docs/plan/11-acceptance-testing-risk.md` §31（首批任务） |

`docs/plan/report.html` 是**自动生成**的，不要手工编辑。改完 md 之后跑 `python3 docs/plan/_build_report.py .` 重新生成。

---

## 3. 怎么跟人说话

这条规则的优先级很高，写在技术规则前面。

**默认读者是一名软件工程专业的本科生。** 他懂编程、Git、数据库这些基础，但不知道你脑子里正在想什么。

### 十条规则

1. **先说结论，再说原因。** 不要从抽象理论开始铺垫。
2. **用普通工程语言。** 能说"订单服务直接改了库存表"，就不要说"跨域所有权冲突"。
3. **不要自己造术语。** 除非是行业里本来就有的标准说法。
4. **第一次出现专业术语时立刻解释。** 例如："这里有循环依赖（A 依赖 B，同时 B 又依赖 A）。"
5. **一句话只讲一个判断。** 不要在一句话里套三层因果。
6. **引用真实代码，不要泛泛而谈。**
   - 不好："存在职责边界问题。"
   - 好："`OrderService.createOrder()` 里直接改了库存，本来这件事应该由 `InventoryService` 做。"
7. **重要问题按四步讲：** 问题是什么 → 为什么会发生 → 会造成什么实际影响 → 应该怎么改。
8. **删掉一个词不损失信息，就删掉它。**
9. **不要写报告腔和论文腔。** 类似"语义所有权漂移""架构收敛""行为契约熵"这种，一律不要。
10. **抽象原则必须落到具体的文件、类、函数或数据流上。**

### 对比

不好：

> 当前架构边界呈现隐式的跨域生命周期耦合，产生语义所有权模糊，并提升编排复杂度。

好：

> 订单服务现在直接控制库存状态。
>
> 这会让两个服务粘在一起：库存逻辑一改，订单服务可能也得跟着改。
>
> 建议订单服务只发一个 `OrderCreated` 事件，由 `InventoryService` 自己决定怎么扣库存。

### 发送前自查

静默检查一遍，不要把检查过程输出出来：

- 有没有哪句话要读两遍才懂？
- 有没有自己造的词？
- 有没有只讲了道理，没说代码里到底发生了什么？
- 有没有能说得更简单的地方？
- 一个没参与过这段开发的人能看懂吗？

有问题就先改，再输出。

---

## 4. 三个冻结件（改之前必须先讨论）

这三样东西是整个项目的地基。后面所有代码——建表、判定、报表、前端展示——都从它们派生。改动的代价随时间指数上升。

**如果你觉得需要改其中任何一条，先停下来说明理由，不要直接改。**

### 4.1 评测语义 —— 已冻结

正式协议文本：[`docs/evaluation-protocol.md`](docs/evaluation-protocol.md)，**FROZEN v1.2（2026-09-02）**，共 79 条编号条款。

`docs/plan/02-evaluation-semantics.md` 讲的是设计理由，协议讲的是精确定义。**两者冲突以协议为准。**

协议冻结后**不得直接修改**，必须走它 §9 的变更流程：提 issue → 至少 1 人 review → 更新协议并升版本号 → 同步更新代码、数据库迁移和规划文档。

CI 里有持续校验（`backend/tests/unit/test_protocol_consistency.py`），改了协议但没重跑真值表会直接让构建失败。

核心内容：

一次评测的结果用三个**互相独立**的字段描述：

- `lifecycle_status`：现在走到哪一步了
- `infra_outcome`：**平台**有没有正确完成这次评测
- `agent_outcome`：**被测 AI** 有没有把 bug 修好

把"AI 失败"和"平台故障"混进同一个字段，解决率就不可信了。这是最容易被写坏的地方。

### 4.2 任务格式（`03-benchmark-spec.md`）

每道题包含：代码仓库快照、issue 描述、`fail_to_pass` 测试（修好之后必须由失败变通过）、`pass_to_pass` 测试（不能被改坏）。

### 4.3 Runner 协议（`04-runner-protocol.md`）

平台和被测 AI 之间的接口：标准输入喂一个 JSON 任务，标准输出的**最后一行**返回一个 JSON 结果，结果里带一段 unified diff（补丁）。

---

## 5. 五个最容易踩的坑

新接手的人（包括 AI）最常在这几处出错。

### 5.1 不要用大模型判断 bug 修没修好

判定必须 100% 由测试结果推导，不能有任何随机性。同一个补丁，今天判和下个月判必须得到一样的结论，否则排行榜不可比。

大模型**只能**用在"分析它为什么没修好"这一步，而且它的输出不能回写判定结果。

### 5.2 被测 AI 改了测试文件，那部分改动必须丢掉

否则它把测试改成 `assert True` 就"通过"了。

两道防线：
1. 生成补丁时，按路径过滤掉测试文件（`tests/`、`conftest.py`、`pytest.ini` 等）。
2. 跑测试前，再强制 `git checkout` 还原一次这些文件，然后才打上官方的测试补丁。

### 5.3 工作区里不能有 base commit 之后的 git 历史

如果直接 `git clone` 再 `checkout`，被测 AI 一句 `git log origin/main` 就能翻到官方的修复代码。

正确做法：`git archive <base_commit>` 导出文件树 → `git init` → 只提交一次。这样工作区里只有一个 commit。

### 5.4 OOM 和超时的退出码都是 137，不能靠退出码区分

这两种情况在语义上完全相反：

- 内存超限（OOM）是**平台**的问题，应该重试。
- 执行超时是**被测 AI** 的问题，应该判定为"没修好"。

判据用 `docker inspect --format '{{.State.OOMKilled}}'`，这个字段实测可靠。

### 5.5 测试用例 ID 必须归一化

`tests/test_a.py::test_x` 和 `./tests/test_a.py::test_x` 是同一个用例，但字符串不相等。匹配不上就会被当成"用例不存在"，进而判定为失败。

这类 bug 不会报错，只会让解决率莫名其妙地偏低，非常难查。必须有专门的单元测试覆盖至少 6 种 ID 写法。

---

## 6. 什么叫"这个任务做完了"

每个任务在 `docs/plan/10-tasks-plan.md` 里有自己的验收标准（AC）。除此之外，下面六条对**所有任务**都适用：

- [ ] AC 全部达成，并且**贴出可以复核的证据**（测试输出、命令回显、截图），不能只说"做完了"
- [ ] 代码通过 PR 合入 `main`，至少 1 人 review
- [ ] 新增或改动的逻辑有对应的自动化测试，CI 全绿
- [ ] 公共接口、领域枚举、复杂算法有中文注释（这是交付硬性要求）
- [ ] 如果改了协议、数据库或枚举：同步更新 `docs/` 和数据库迁移脚本，且迁移可以回滚
- [ ] 如果引入了新的环境依赖：更新部署文档和 `scripts/check_env.py`

第一条最重要。"贴证据"把"我觉得做完了"变成"这是它工作的样子"，成本只有一次复制粘贴。

---

## 7. Git 约定

### 分支

- `main`：始终可运行、CI 绿。受保护，不能直接 push。
- 特性分支：`feat/E2-T2-container-runner`，即 `<类型>/<任务ID>-<短描述>`，**存活不超过 2 天**。
- 合并方式：**Squash merge**，保持 `main` 线性。

不要用 GitFlow。4 周的项目开 `develop`/`release` 分支只会增加合并负担。

### Commit 信息

用 Conventional Commits，scope 写 Epic 编号：

```
feat(E2): 容器执行器支持 pids-limit 与 OOM 判定
fix(E4): 修正 pytest 用例 ID 归一化对参数化用例的处理
test(E2): 补齐四条沙箱负例
docs(plan): 回填沙箱实测结论
chore(E0): 引入 import-linter 模块边界规则
```

类型：`feat` / `fix` / `test` / `docs` / `refactor` / `chore` / `perf`

### 绝对不能提交进仓库的东西

| 路径 | 为什么 |
|:---|:---|
| `var/workspaces/**` | 每次评测物化的代码工作区，数量随运行次数线性增长 |
| `var/artifacts/**` | 日志、补丁、测试报告，单次实验几百 MB |
| `var/mirrors/**` | Git 镜像仓库，单个仓库几百 MB |
| `datasets/exports/*.jsonl` | 数据集导出文件，MB 级 |
| `.env` / `*.env` / `**/secrets*` | **API 密钥**。这个项目要接好几个大模型服务商，泄漏风险高 |

必须提交的：`datasets/golden/**`（测试基石，KB 级）、`tests/fixtures/**`、`images/**/Dockerfile*`、`alembic/versions/**`、`docs/**`。

### 跑正式实验之前，工作区必须是干净的

每次实验都会把当前代码的 git commit id 记进结果里。如果工作区有未提交的改动，这个 id 就不能唯一代表代码状态，"可复现"就是假的。

所以：`git status --porcelain` 不为空时，拒绝启动正式实验。调试时可以用 `--allow-dirty` 绕过，但结果会被标记为 `dirty`，**不能进排行榜**。

---

## 8. 代码规范

- **语言**：后端 Python 3.11+，前端 TypeScript
- **注释**：公共接口、领域枚举、复杂算法必须有中文注释
- **标识符用英文**：变量名、函数名、类名、测试函数名一律用英文。中文只用在注释、文档字符串和文档里。
  原因：中文标识符会被 ruff 的命名规则判为不合规（N802/N806），而且在 Python 生态里不常见。
  "中文注释"是交付要求，"中文变量名"不是
- **架构**：模块化单体（一个代码库，API 和 Worker 是两个不同的启动入口）
- **模块依赖方向**（用 import-linter 在 CI 里强制）：

```
api → evaluation / benchmark / report
    → runner / sandbox / judge / attribution
    → storage / infrastructure
    → domain
```

`domain` 不依赖任何其他模块。`sandbox` 不能依赖 `runner`（是 runner 用 sandbox，不能反过来）。

- **目录结构**：见 `docs/plan/11-acceptance-testing-risk.md` §30

---

## 9. 测试要求

### 三条"哨兵测试"

这三条把"这个基准可不可信"变成了可以自动验证的断言，比人肉相信可靠得多。

1. **Oracle 哨兵**：用官方的正确补丁跑整个数据集，解决率必须 **100%**。不是 100% 就说明有坏题或者判定引擎有 bug。**每次发布数据集前必须跑，作为发布门槛。**
2. **Noop 哨兵**：用空补丁跑整个数据集，解决率必须 **0%**。不是 0% 说明有的题目在修复前测试就已经通过了。
3. **确定性哨兵**：同一个补丁重复判定 3 次，每条用例的状态必须完全一致。

### 沙箱负例（已在开发机验证通过，见 `05-sandbox.md` §10.3）

内存炸弹要被 OOM 杀掉、fork 炸弹要被进程数限制拦住、死循环要被按时杀掉且不留残留容器、`--network none` 下要真的连不上网。

### 分层

单元测试和集成测试每次提交都跑（3 分钟内）。带 `@pytest.mark.docker` 标记的每日跑。消耗大模型额度的适配器测试手动触发。

---

## 10. 开发环境须知

开发机是 Windows 11 + WSL2（Ubuntu 24.04）。有几个已经踩过的坑：

**Docker 已装好**：29.7.2，原生 engine（不是 Docker Desktop），systemd 托管，开机自启，免 sudo。

**网络要走代理**，而且必须配三个地方，少一个就表现为"拉不动镜像"：

| 配置点 | 位置 |
|:---|:---|
| dockerd 代理 | `/etc/systemd/system/docker.service.d/http-proxy.conf` |
| 镜像加速 | `/etc/docker/daemon.json` 的 `registry-mirrors` |
| 容器内代理 | `~/.docker/config.json` 的 `proxies.default` |

代理地址是 WSL 的网关 IP，**`wsl --shutdown` 之后可能会变**。变了之后上面三处都要同步更新。用 `ip route show default` 取，不要写死。

**不要在 Docker Desktop 里为这个 WSL 发行版开启集成。** 开了之后它会接管 `/var/run/docker.sock`，导致 docker 命令连到另一个守护进程上，表现是镜像和容器"凭空消失"。

**跑最终实验前退出 Docker Desktop。** 所有 WSL 发行版共用 `.wslconfig` 里的内存额度，Docker Desktop 开着会占掉留给测试容器的内存。

资源：16 vCPU / 11 GiB（由 `.wslconfig` 控制）/ 920 GB 可用磁盘。

---

## 11. 明确不要做的事

这个项目非常容易过度开发。下面这些一律不做：

Kubernetes、微服务拆分、消息队列中间件、分布式调度、自研容器运行时、多租户、完整权限系统、微调专用判定模型、跑 SWE-Bench 全量 500 题、接入 10 个以上 AI 助手、支持所有编程语言、WebSocket 实时轨迹、花哨的 UI 动画。

完整清单和理由见 `docs/plan/11-acceptance-testing-risk.md` §29。

另外三条：

- **不要在第一周碰真实的 AI 助手。** 第一周的目标是用 Mock（假的 AI）把整条链路跑通。
- **不要为了"看起来专业"引入技术。** 判断标准是"能不能在 4 周内交付并演示"。
- **不要绕过冻结件。** 需要改就先说，不要直接改。

---

## 12. 常用命令

```bash
# 重新生成规划报告（改完 docs/plan/*.md 之后）
python3 docs/plan/_build_report.py .

# 环境自检（E0-T2 完成后可用）
python3 scripts/check_env.py

# 确认连的是原生 docker 而不是 Docker Desktop
docker info --format '{{.Name}} {{.DockerRootDir}}'
# 期望输出：DESKTOP-D3QQNH3 /var/lib/docker
```

```bash
# 后端（都在仓库根目录跑，Makefile 会自己 cd 进 backend）
make install         # 装依赖 + 装提交钩子
make check           # 提交前跑一遍：lint + 类型 + 模块边界 + 测试
make test            # 只跑测试（跳过需要 Docker 和真实大模型的）

# 数据库（端口 5433，不是 5432 —— 避开这台机器上别的项目）
make db-up           # 起本地 Postgres 容器
make migrate         # 升到最新
make migrate-check   # 检查模型和迁移有没有对不上
make seed            # 写入三个哨兵 Agent 的种子数据
make db-psql         # 连进去看
make db-reset        # 删掉容器和数据重来
```

前端命令等前端脚手架建好后补充。
