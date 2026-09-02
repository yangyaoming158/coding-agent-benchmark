# 参与开发

> 开发规则的完整版在 [`AGENTS.md`](./AGENTS.md)。这份文档只讲**怎么动手**：
> 环境怎么装、一次改动怎么走完流程、提交前该检查什么。
> 规则的**理由**在 AGENTS.md 里，这里不重复。

---

## 1. 装环境

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

---

## 8. 常用命令

```bash
make help            # 列出全部命令

make check           # 提交前跑一遍：lint + 类型 + 模块边界 + 测试
make test            # 只跑测试
make dev             # 同时起后端 API 和前端

make db-up           # 起 Postgres
make db-reset        # 删掉容器和数据重来
make db-psql         # 连进去看
make migrate         # 升到最新
make migrate-check   # 检查模型和迁移有没有对不上
make seed            # 写入哨兵 Agent

make report          # 改完 docs/plan/*.md 之后重新生成规划报告
```
