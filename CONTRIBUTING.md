# 参与开发

> 开发规则的完整版在 [`AGENTS.md`](./AGENTS.md)。这份文档只讲**怎么动手**：
> 环境怎么装、一次改动怎么走完流程、提交前该检查什么。
> 规则的**理由**在 AGENTS.md 里，这里不重复。

---

## 1. 装环境

### 先看操作系统：原生 Windows 跑不了

需要 **Linux**，或者 **Windows + WSL2**（在 WSL 里面开发，不是在 Windows 里）。

这不是偏好问题，是两处硬阻塞：

| 阻塞 | 在哪 | 表现 |
|:---|:---|:---|
| `os.getuid()` / `os.getgid()` | `app/sandbox/container.py` 的 `default_container_user()` | Windows 上这两个函数**根本不存在**，起容器第一步就 `AttributeError` |
| 绑定挂载的路径 | 同文件，`_create_kwargs()` 直接把宿主路径给 docker | Windows 给出来的是 `C:\Users\...`，docker 要的是 `/c/Users/...` |

第一条在每次起容器的必经之路上，绕不过去。修它也只是把问题推到第二条，
再往后还有 `make`、`scripts/dev_db.sh`、Worker 靠 SIGTERM 的优雅停机 ——
这些都要 POSIX 环境。**为一个 4 周的项目做 Windows 移植不划算。**

> **换行符不用操心，已经处理过了。** Git for Windows 默认 `core.autocrlf=true`
> 会改写换行符，对这个项目那是正确性问题（补丁是 unified diff，`content_hash`
> 算的就是文件内容）。但仓库根目录的 `.gitattributes` 已经钉了 `* text=auto eol=lf`，
> 而 harness 自己的 git 调用还额外把 `GIT_CONFIG_GLOBAL` 指向 `/dev/null`
> （`app/sandbox/git_cli.py`，理由见 `docs/plan/05-sandbox.md` §10.7）。
> **别去改这两处"优化"掉**，它们是有代价买来的。

**好消息是不用换操作系统。** WSL2 是 Windows 自带的功能，不是另一台机器：

```powershell
wsl --install -d Ubuntu-24.04     # 管理员 PowerShell，装完重启
```

装好之后**所有开发都在 WSL 里做**：代码放在 WSL 的文件系统里（`~/projects/...`），
不要放在 `/mnt/c/...`——跨文件系统的 IO 慢一个数量级，而这个项目要频繁物化代码工作区。

VS Code 装 **WSL 扩展**就能直接在里面开发，体验和本地一样。

### Docker：一台机器上只能有一个 daemon

装 Docker 有两条路，**选一条，别两条都装**：

- **在 WSL 里装原生 docker engine**（本项目开发机用的就是这个）。省内存 —— Docker Desktop
  会额外常驻一个 WSL 发行版和一堆 GUI 进程，白吃 1~2 GB。这个项目内存很紧
  （沙箱并发 5 × 1.5 GB + 基线 3.2 GB 已经逼近 11 GB 的上限），这 1~2 GB 是有代价的
- **Docker Desktop + WSL 集成**。装起来最省事，但**如果 WSL 里已经有原生 dockerd，
  千万别开集成** —— 开了之后它接管 `/var/run/docker.sock`，docker 命令连到另一个
  守护进程上，表现是镜像和容器"凭空消失"（这个坑已经踩过）

确认自己连的是哪一个：

```bash
docker info --format '{{.Name}} {{.DockerRootDir}}'
```

### 其余依赖

需要：Python 3.11+、[uv](https://docs.astral.sh/uv/)、Docker、Node 20+。

```bash
make install     # 装后端依赖 + 装好两个 git 钩子
make db-up       # 起本地 Postgres（容器，端口 5433）
make migrate     # 建表
make seed        # 写入三个哨兵 Agent
make check       # 全套检查，应该全绿
```

`make check` 不绿就先别往下走 —— 那说明环境没装对，不是代码有问题。

**端口是 5433 不是 5432**。这台开发机上还有别的项目在用 Postgres，
抢同一个端口会让两边都起不来，而且报错信息完全看不出是端口冲突。

跑 `python3 scripts/check_env.py` 可以自检环境，它把踩过的坑固化成了检查项。

---

## 1.5 接手一份已有的环境

`git clone` 给你的是代码和文档。下面这些**拿不到**，得单独交接：

| 东西 | 大小 | 怎么来 |
|:---|---:|:---|
| `.env`（各家大模型 API Key） | — | **走私密渠道找项目负责人要**，绝不能进仓库 |
| `var/cache/`（GitHub 响应 + 大模型回答的缓存） | ~3 MB | **一定要拿**。没有它，重跑挖掘和预筛要真花钱、真消耗 GitHub 配额；有它就是纯走缓存 |
| `var/mirrors/`（git 镜像） | ~55 MB | 拿，或者自己重新 clone（过代理很慢） |
| `var/build-snapshots/` | ~16 MB | 同上 |
| Docker 镜像（`bench-base` + 每个环境一个） | 每个 ~840 MB | **不传，自己建**：`python -m cli.images build`。小仓库几分钟，大仓库十分钟出头 |
| 开发库里的数据（题目、数据集版本） | — | **不传，自己重灌**：照 `AGENTS.md` 第 12 节的重灌规程跑一遍，前提是 `var/cache/` 在 |

交接包这样打（实测 69 MB，聊天工具传得动）：

```bash
tar czf handoff.tar.gz var/cache var/mirrors var/build-snapshots
```

`.env` **不要**放进去。

拿到之后的顺序：

```bash
tar xzf handoff.tar.gz            # 解到仓库根目录
cp .env.example .env              # 再把要来的 Key 填进去
make install && make db-up && make migrate
python3 scripts/check_env.py      # 环境自检，把踩过的坑固化成了检查项
# 然后照 AGENTS.md 第 12 节重灌数据库
```

### 多个人（或多个 AI 助手）同时开发时

- **动手前先在 GitHub 上把对应 issue 分配给自己。** issue 是
  `scripts/sync_issues.py` 从 `docs/plan/10-tasks-plan.md` 生成的，一张卡一个。
  不认领的话，两个 AI 助手同时做同一张卡是完全可能的 —— 它们不会互相打招呼
- **每人一台自己的机器、自己的数据库。** 开发库是本机的，而且
  **跑任何一个集成测试都会把它连上的库清空**（`AGENTS.md` 第 9 节）。共用一个库等于互相清数据
- **分支和 PR 一条都不能省。** AI 助手不会像人一样"等一下先同步"，
  它们会自信地覆盖。分支加 PR 是唯一能把并行改动序列化的机制

---

## 2. 一次改动的完整流程

```bash
git switch -c feat/E2-T2-container-runner    # <类型>/<任务ID>-<短描述>
# 写代码，写测试
make check                                    # 提交前必跑
git commit -m "feat(E2): 容器执行器支持 pids-limit 与 OOM 判定"
git push -u origin feat/E2-T2-container-runner
gh pr create                                  # PR 模板里有 DoD 清单，逐条勾
```

分支**存活不超过 2 天**。合并用 squash，保持 `main` 线性。

不要开 `develop` / `release` 分支。4 周的项目用 GitFlow 只会增加合并负担。

---

## 3. 提交前要过的检查

`make check` 跑四件事，任何一件红了都不该提交：

| 命令 | 检查什么 | 红了通常意味着 |
|:---|:---|:---|
| `ruff check` | 代码风格、未用变量、命名 | 照着提示改就行 |
| `mypy --strict` | 类型 | 缺类型标注，或者真的类型错了 |
| `lint-imports` | 模块之间的依赖方向 | **架构越界**，见下一节 |
| `pytest` | 测试 | — |

git 钩子还会另外拦两件事：

- **提交里含密钥** —— 支持 OpenAI / Anthropic / GitHub / AWS / 阿里云等格式。
  密钥只放 `.env`（已被 `.gitignore` 排除）。
- **提交信息不符合 Conventional Commits** —— 格式是 `<类型>(<Epic 编号>): <描述>`。

这两条钩子本身有测试（`backend/tests/unit/test_repo_guards.py`）：
"配好了"和"真的会拦"是两回事，中间任何一环配错，日常开发都不会有异常。

---

## 4. 模块依赖方向

```
api → evaluation / benchmark / report
    → runner / sandbox / judge / attribution
    → storage / infrastructure
    → domain
```

上层可以依赖下层，下层**不可以**反向依赖。另外两条：

- `domain` 不依赖任何其他模块。它是评测协议的代码化表达，必须能独立读懂。
- `sandbox` 不能依赖 `runner`。是 runner 用 sandbox，不能反过来。
- `judge` 不能依赖 `runner` 和 `attribution`。判定必须独立于"补丁是谁产生的"。

规则写在 `backend/pyproject.toml` 的 `[tool.importlinter]` 里，CI 强制。
越界的 import 会被 `lint-imports` 直接拦下，不是靠 code review 靠人眼看。

---

## 5. 三样东西改之前必须先讨论

它们已经冻结，改动会波及一大片代码：

| 冻结件 | 在哪 | 改动流程 |
|:---|:---|:---|
| **评测协议 v1.2** | `docs/evaluation-protocol.md` | 提 issue → 至少 1 人 review → 升版本号 → 同步改代码、迁移和规划文档（协议 §9） |
| **任务 Schema** | `docs/plan/03-benchmark-spec.md` §7.1 | 同上 |
| **数据库枚举** | `backend/app/domain/enums.py` 第一部分 | 跟着协议走，不能单独改 |

协议里的枚举取值有**单元测试直接解析协议原文比对**（`test_enum_consistency.py`）。
改了代码没改协议，或者改了协议没重跑真值表，CI 都会红。

---

## 6. 代码规范

- **注释用中文**，公共接口、领域枚举、复杂算法必须有。这是交付硬性要求。
- **标识符用英文**。变量名、函数名、类名、测试函数名一律英文。
  中文标识符会被 ruff 的命名规则判为不合规，在 Python 生态里也不常见。
  "中文注释"是要求，"中文变量名"不是。
- 行宽 100。
- 注释写**为什么**，不写**是什么**。代码已经说了是什么。

一个好注释长这样：

```python
# 用 IS NOT DISTINCT FROM 而不是 = ：agent_outcome 可为空，而 SQL 里
# `NULL = '值'` 的结果是 NULL 不是 FALSE，整串 OR 会变成 NULL，
# 而 CHECK 约束遇到 NULL 是放行的。这个坑已经在本机复现过。
```

一个没用的注释长这样：

```python
# 把 agent_outcome 和值比较
```

---

## 7. 测试

| 类型 | 什么时候跑 | 怎么标记 |
|:---|:---|:---|
| 单元 + 集成 | 每次提交 | 无标记，3 分钟内跑完 |
| 需要数据库 | 每次提交（连不上就跳过） | `@pytest.mark.db` |
| 需要 Docker | 每日夜间 | `@pytest.mark.docker` |
| 消耗大模型额度 | 手动触发 | `@pytest.mark.agent` |
| 单条超过 10 秒 | 跟着所在层级 | `@pytest.mark.slow` |

写测试时注意一件事：**证明约束会拦，比证明正常路径能过更重要**。
正常路径出问题很快就会被发现，约束失效则会一直静默，直到出报告时才暴露。

### 测试跑在独立的库上，别绕开 Makefile

集成测试的第一件事是 `downgrade base` + `upgrade head`——把它连上的库整个抹掉重建。
所以 `make test` / `make check` / `make test-docker` / `make test-all` 都指向
独立的测试库 `bench_test`（库不存在会自动建），开发库 `bench` 不受影响。

**绕开 Makefile 直接 `uv run pytest` 就会连回开发库，一条集成测试就能把它清空。**
真要手动跑，自己把库指过去：

```bash
cd backend
BENCH_DATABASE_URL=postgresql+psycopg://bench:bench@localhost:5433/bench_test \
  uv run pytest tests/integration/test_mining_persistence.py
```

库名里不含 `test` 时会打一条醒目的警告（不拒绝——CI 的库名可能不一样）。
看见那条警告，就说明这一跑正在清开发库；重灌一遍要二十分钟。
CI 换库名：`make test TEST_DATABASE_URL=postgresql+psycopg://...`。

---

## 8. 常用命令

```bash
make help            # 列出全部命令

make check           # 提交前跑一遍：lint + 类型 + 模块边界 + 测试
make test            # 只跑测试
make dev             # 同时起后端 API 和前端

make db-up           # 起 Postgres
make db-test         # 建测试库 bench_test（make test 会自动调）
make db-reset        # 删掉容器和数据重来
make db-psql         # 连进去看
make migrate         # 升到最新
make migrate-check   # 检查模型和迁移有没有对不上
make seed            # 写入哨兵 Agent

make report          # 改完 docs/plan/*.md 之后重新生成规划报告
```
