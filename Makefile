# 常用命令。所有 Python 命令都通过 uv 跑，不需要手动激活虚拟环境。
# 用 bash 不用 sh：dev 目标要用 trap 和 kill 0 一次收掉两个子进程。
SHELL := /bin/bash

.PHONY: help install lint format type imports test test-docker test-all check env clean \
        db-up db-down db-reset db-psql db-test migrate migrate-down migrate-check seed \
        seed-tasks validate-tasks survey survey-measure mine mine-report prescreen prescreen-clean prescreen-report \
        promote-probe promote-assemble promote-review promote-report \
        dataset-stage dataset-gate dataset-publish dataset-show dataset-verify quality-report \
        swebench-fetch swebench-sample swebench-warm swebench-pull swebench-fetch-blobs swebench-load swebench-build \
        swebench-mirror swebench-import swebench-validate swebench-report \
        worker enqueue queue stress-sweep stress-hold stress-oom \
        dev dev-api dev-web web-install web-lint web-build gen-api report schema \
        golden golden-verify images images-aider images-claude-code test-agent \
        images-base images-envs images-list images-gc

BACKEND := backend
FRONTEND := frontend
# Golden 题的测试镜像标签。测试执行器的默认值和这里对齐（app/evaluation/test_executor.py）。
GOLDEN_IMAGE := bench-golden:py311
# Aider 的 Agent 镜像。适配器里的默认值和这里对齐（app/runner/adapters/aider.py），
# 数据库里那份在 agent_configs.params["image"]
AIDER_IMAGE := bench-agent:py311-aider
# Claude Code 的 Agent 镜像。同样在 app/runner/adapters/claude_code.py 里有默认值
CLAUDE_CODE_IMAGE := bench-agent:py311-claude-code
UV := cd $(BACKEND) && uv run

# ── 测试跑在独立的库上（#88）────────────────────────────────
# 跑**任何一个**集成测试都会 `downgrade base` + `upgrade head`，整库连表带数据抹掉重建。
# 指向开发库的话，`make check` 乃至单跑一个集成测试文件都会把挖好的候选、验完的题
# 一起清掉（2026-09-09 排查过两次，2026-09-10 又清掉过 31 道题）。
# 换个库名就隔开了：开发库 bench 不动，测试跑 bench_test。CI 想换库名：
#   make test TEST_DATABASE_URL=postgresql+psycopg://...
# 库名里要带 test —— 不带的话 tests/integration/conftest.py 会打一条醒目的警告。
TEST_DATABASE_URL := postgresql+psycopg://bench:bench@localhost:5433/bench_test
UV_TEST := cd $(BACKEND) && BENCH_DATABASE_URL=$(TEST_DATABASE_URL) uv run

help:                ## 显示这份帮助
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:             ## 安装前后端依赖并装好提交钩子
	cd $(BACKEND) && uv sync
	$(UV) pre-commit install
	$(UV) pre-commit install --hook-type commit-msg
	cd $(FRONTEND) && npm install

# scripts/ 和 docs/ 下的 Python 也要检查。原来只在 backend/ 里跑 ruff，
# 那两个目录是盲区 —— scripts/check_commit_msg.py 里的中文标识符就是这么活下来的
LINT_PATHS := . ../scripts ../docs

# `make enqueue` 的默认参数，命令行可以覆盖：make enqueue AGENT=noop NAME=noop-smoke
#
# 这些默认值一律用 `:=` 不用 `?=`。`?=` 的语义是"没定义才赋值"，而 make 把**环境变量
# 也算已定义** —— WSL 里恰好有个叫 NAME 的环境变量（Windows 侧透过来的计算机名），
# 于是 `make enqueue` 建出来的实验名成了 DESKTOP-D3QQNH3，实验列表里一排主机名
# 谁也分不清（#89，2026-09-11 在 `make -n enqueue` 的回显里看见的）。
# `:=` 不看环境变量，而命令行赋值仍然优先（命令行 > := > 环境变量），
# 所以 `make enqueue NAME=noop-smoke` 照常工作。
AGENT := oracle
NAME := adhoc

# 建实验的两个目标（dataset-gate / enqueue）在工作区不干净时会被协议 C-27 拦下。
# 开发期确实要带着未提交改动跑一把时：make enqueue ALLOW_DIRTY=1
# 放行的实验会被标 dirty=true，按 C-28 不得进排行榜（E5-T4）。
DIRTY_FLAG := $(if $(ALLOW_DIRTY),--allow-dirty,)

lint:                ## 代码检查（含 scripts/ 和 docs/ 下的脚本）
	$(UV) ruff check $(LINT_PATHS)
	$(UV) ruff format --check $(LINT_PATHS)

format:              ## 自动格式化
	$(UV) ruff format $(LINT_PATHS)
	$(UV) ruff check --fix $(LINT_PATHS)

type:                ## 类型检查
	$(UV) mypy app cli

imports:             ## 模块边界检查（§14.2 的依赖方向）
	$(UV) lint-imports

# 测试库不存在就建、建好升到最新。数据库没起来时只打一条提示：
# 单元测试不需要库，需要库的用例连不上会自己跳过，不该在这里就把 make 干掉。
db-test:             ## 准备测试库 bench_test（建库 + 升到最新，测试目标会自动调）
	@./scripts/dev_db.sh test-db && $(UV_TEST) alembic upgrade head >/dev/null \
	  || echo "⚠ 测试库没准备好：需要数据库的用例会跳过（起库：make db-up）"

test: db-test        ## 快速测试（不含需要 Docker 和真实大模型的）
	$(UV_TEST) pytest -m "not docker and not agent"

# 要排掉 agent：那些用例会真的调大模型，是要花钱的。
# 夜间跑一次 test-docker 就把额度烧掉一截，而且没人会注意到
test-docker: db-test ## 只跑需要 Docker 的沙箱测试（不含花钱的）
	$(UV_TEST) pytest -m "docker and not agent"

test-agent: db-test  ## 跑会真的调用大模型的用例（要 API Key，会花钱，手动触发）
	$(UV_TEST) pytest -m agent

test-all: db-test    ## 全部测试
	$(UV_TEST) pytest

check: lint type imports test   ## 提交前跑一遍：检查 + 类型 + 边界 + 测试

dev:                 ## 同时起后端（:8000）和前端（:3000）
	@echo "后端 http://localhost:8000/docs   前端 http://localhost:3000"
	@echo "Ctrl-C 一次同时停掉两个"
	@trap 'kill 0' EXIT INT TERM; \
	  ( cd $(BACKEND) && uv run uvicorn app.main:app --reload --port 8000 ) & \
	  ( cd $(FRONTEND) && npm run dev ) & \
	  wait

dev-api:             ## 只起后端
	$(UV) uvicorn app.main:app --reload --port 8000

dev-web:             ## 只起前端
	cd $(FRONTEND) && npm run dev

web-install:         ## 装前端依赖
	cd $(FRONTEND) && npm install

web-lint:            ## 前端检查（eslint + tsc）
	cd $(FRONTEND) && npm run lint && npm run typecheck

web-build:           ## 前端生产构建
	cd $(FRONTEND) && npm run build

gen-api:             ## 从后端 OpenAPI 生成前端类型（需要后端在跑）
	cd $(FRONTEND) && npm run gen:api

db-up:               ## 起本地 Postgres（端口 5433）
	./scripts/dev_db.sh up

db-down:             ## 停 Postgres，数据保留
	./scripts/dev_db.sh down

db-reset:            ## 删掉容器和数据，重新起一个空库
	./scripts/dev_db.sh reset

db-psql:             ## 连进数据库看
	./scripts/dev_db.sh psql

migrate:             ## 把数据库升到最新
	$(UV) alembic upgrade head

migrate-down:        ## 回滚到空库
	$(UV) alembic downgrade base

migrate-check:       ## 检查模型和迁移有没有对不上
	$(UV) alembic check

seed:                ## 写入哨兵 Agent 的种子数据
	$(UV) python -m cli.seed

seed-tasks:          ## 把 Golden 题写进库（开发用，不跑验证流水线）
	$(UV) python -m cli.queue seed-golden

validate-tasks:      ## 跑八步验证流水线（要 Docker），把结论写回 benchmark_tasks
	$(UV) python -m cli.validate run

survey:              ## 仓库选型：查 GitHub 并打分（要网络；容器实测另跑 survey-measure）
	$(UV) python -m cli.survey probe

survey-measure:      ## 仓库选型第二段：容器里实测安装与测试耗时（要 Docker，慢）
	$(UV) python -m cli.survey measure

# 默认挖一个已经有 env 镜像的仓库：挖出候选之后能直接接上验证流水线跑通闭环。
# 换仓库：make mine MINE_REPO=sqlfluff/sqlfluff
MINE_REPO := pallets/click

mine:                ## GitHub 挖掘：merged PR + 关联 issue → task_candidates（要网络）
	$(UV) python -m cli.mine run --repo $(MINE_REPO)

mine-report:         ## 候选产出率报表（读 datasets/mining/ 的存档，不联网）
	$(UV) python -m cli.mine report

prescreen-clean:     ## 候选清洗：脱敏 + 拆补丁 + 抽候选 F2P（要网络，不花钱）
	$(UV) python -m cli.prescreen clean

# 会真的调大模型、会花钱。先用 SCORE_LIMIT 小批量试，确认分数分布合理再全量。
SCORE_LIMIT :=

prescreen:           ## LLM 预筛打分（**要 API Key，会花钱**）
	$(UV) python -m cli.prescreen score $(if $(SCORE_LIMIT),--limit $(SCORE_LIMIT),)

prescreen-report:    ## 清洗与分数分布（读库，不联网不花钱）
	$(UV) python -m cli.prescreen report

# ── E8-T2：候选 → 题目 ─────────────────────────────────────
# 顺序是 probe → assemble → images build（写回 digest）→ validate-tasks → export-review
PROBE_LIMIT :=

promote-probe:       ## 探测轮：实测证伪 F2P、派生 P2P（要 Docker，一条起两个容器）
	$(UV) python -m cli.promote probe $(if $(PROBE_LIMIT),--limit $(PROBE_LIMIT),)

promote-assemble:    ## 把探测通过的候选组装成题目写进 benchmark_tasks
	$(UV) python -m cli.promote assemble

promote-review:      ## 导出人工终审对照表（CSV）
	$(UV) python -m cli.promote export-review

promote-report:      ## 漏斗报表：每一层剩多少、掉队的为什么
	$(UV) python -m cli.promote report --save

# ── E1-T7：SWE-bench Verified 官方题导入（校准集，slug 单独是 swebench-verified-subset）──
# 顺序是 fetch → sample → pull（慢，38.9 GB，可反复续跑）→ mirror → import → validate → report，
# 之后走下面 E1-T6 那套：make dataset-stage DATASET=swebench-verified-subset 等等。
SWEBENCH_SEED := 20260915
SWEBENCH_N := 100
SWEBENCH_ARGS := --seed $(SWEBENCH_SEED) --n $(SWEBENCH_N)
SWEBENCH_JOBS := 2

swebench-fetch:      ## 拉官方数据集 500 行到 var/cache/swebench/（要网络，不花钱）
	$(UV) python -m cli.swebench fetch

swebench-sample:     ## 固定种子分层抽样，名单写进 datasets/swebench/（离线）
	$(UV) python -m cli.swebench sample $(SWEBENCH_ARGS)

swebench-warm:       ## 预热镜像站：每层碰几秒让它先去 Docker Hub 缓存（直连镜像站，不走代理）
	$(UV) python -m cli.swebench warm $(SWEBENCH_ARGS)

swebench-pull:       ## 拉抽中题的官方镜像并探测（要网络 + Docker，断了重跑接着拉）
	$(UV) python -m cli.swebench pull $(SWEBENCH_ARGS)

swebench-fetch-blobs: ## 用 Windows 的 curl.exe 把镜像的层下到 D 盘（WSL 到宿主那一跳太慢时用）
	$(UV) python -m cli.swebench fetch-blobs $(SWEBENCH_ARGS)

swebench-load:       ## 把 D 盘上的层拼成 OCI 布局 docker load 进来，再探测登记
	$(UV) python -m cli.swebench load $(SWEBENCH_ARGS)

swebench-build:      ## 按官方配方本机建镜像（依赖走清华源；JOBS 并行数）
	$(UV) python -m cli.swebench build $(SWEBENCH_ARGS) --jobs $(SWEBENCH_JOBS)

swebench-mirror:     ## 按 base_commit 浅拉 git 镜像（要网络）
	$(UV) python -m cli.swebench mirror $(SWEBENCH_ARGS)

swebench-import:     ## 组装题目 + 环境规格入库（只收镜像已拉到的）
	$(UV) python -m cli.swebench import $(SWEBENCH_ARGS)

swebench-validate:   ## 八步验证，只跑声明的用例（要 Docker）
	$(UV) python -m cli.validate run --dataset swebench-verified-subset --scope declared

swebench-report:     ## 导入漏斗（AC 8），--save 落 datasets/swebench/
	$(UV) python -m cli.swebench report $(SWEBENCH_ARGS) --save

# ── E1-T6：数据集版本化与发布 ──────────────────────────────
# 顺序是 stage → gate → worker（跑门禁）→ publish。DATASET 和 SLUG 可以覆盖：
#   make dataset-stage DATASET=benchmark-dev
DATASET := benchmark-dev
SLUG := $(DATASET)

dataset-stage:       ## 冻快照：把该 dataset 全部 VALID 的题写进 benchmark_set_items
	$(UV) python -m cli.dataset stage --dataset-id $(DATASET) --slug $(SLUG)

dataset-gate:        ## 建 Oracle / Noop 两个门禁实验并投队列（脏工作区加 ALLOW_DIRTY=1）
	$(UV) python -m cli.dataset gate --slug $(SLUG) $(DIRTY_FLAG)

dataset-publish:     ## 查门禁（C-50：Oracle 100% / Noop 0%），过了才发布
	$(UV) python -m cli.dataset publish --slug $(SLUG) $(DIRTY_FLAG)

dataset-show:        ## 看有哪些数据集版本
	$(UV) python -m cli.dataset show

dataset-verify:      ## 拿已发布版本的快照比对现在的题库，报漂移（纯查询）
	$(UV) python -m cli.dataset verify --slug $(SLUG)

# ── E8-T5：数据集质量报告 ───────────────────────────────────
# 默认看 benchmark-cn-v1 + swebench-verified-subset 的最新已发布版；只数发布版里的题。
#   make quality-report QUALITY_ARGS="--set benchmark-cn-v1@v1"
quality-report:      ## 来源 / 语言 / 难度 / 漏斗，--save 落 datasets/quality/
	$(UV) python -m cli.quality report $(QUALITY_ARGS) --save

# ── E9-T2：并发压测 ────────────────────────────────────────
# 三条子命令：sweep 跑真实负载扫并发、hold 看满载容器把宿主压到哪、
# oom 数 .State.OOMKilled 漏报几次（issue #85 等这个数）。
# 产物（CSV + Worker 日志）落 var/stress/，不提交。
STRESS_SANDBOX := 5
STRESS_ROUNDS := 5

stress-sweep:        ## 按 STRESS_SANDBOX 跑一组真实负载压测（要 Docker + 数据库）
	$(UV) python -m cli.stress sweep --sandbox $(STRESS_SANDBOX) --rounds $(STRESS_ROUNDS)

stress-hold:         ## N 个容器同时吃满内存，看宿主水位（要 Docker）
	$(UV) python -m cli.stress hold --parallel $(STRESS_SANDBOX)

stress-oom:          ## 故意制造 OOM，数 OOMKilled 漏报率（要 Docker）
	$(UV) python -m cli.stress oom --parallel $(STRESS_SANDBOX)

worker:              ## 起一个 Worker 进程（Ctrl-C 优雅停机）
	$(UV) python -m app.worker

enqueue:             ## 建一次实验，把某一版数据集的题投进队列（AGENT=oracle NAME=adhoc）
	$(UV) python -m cli.queue enqueue --agent $(AGENT) --name $(NAME) --set $(SLUG) $(DIRTY_FLAG)

queue:               ## 看作业队列现状
	$(UV) python -m cli.queue status

runs:                ## 看实验进度（细账：python -m cli.experiment status --run N）
	$(UV) python -m cli.experiment status

env:                 ## 开发环境自检
	python3 scripts/check_env.py

report:              ## 重新生成规划报告 HTML
	python3 docs/plan/_build_report.py .

schema:              ## 重新导出 schemas/ 下的三份 JSON Schema（题目 + Runner 协议）
	$(UV) python -m cli.task schema
	$(UV) python -m cli.runner schema

golden:              ## 从 datasets/golden/sources/ 生成任务 JSON 和本地镜像
	$(UV) python -m cli.golden build

golden-verify:       ## 对每道 Golden Task 跑六步验证
	$(UV) python -m cli.golden verify

images:              ## 建 Golden 题的手写测试镜像（E4-T2 用；env_spec 没填 image_tag 时的兜底）
	docker build -t $(GOLDEN_IMAGE) images/golden

images-base:         ## 建第一层 bench-base（E2-T3）
	$(UV) python -m cli.images build --base-only

images-envs:         ## 建全部环境镜像（第一层 + images/envs/*.json，写回 environment_specs）
	$(UV) python -m cli.images build

images-list:         ## 列出构建器建出来的镜像（tag / digest / 大小 / 磁盘水位）
	$(UV) python -m cli.images list

images-gc:           ## 列出可回收的镜像（默认只列不删，真删加 ARGS=--yes）
	$(UV) python -m cli.images gc $(ARGS)

images-aider:        ## 建 Aider 的 Agent 镜像（要先有 $(GOLDEN_IMAGE)）
	docker build -t $(AIDER_IMAGE) images/aider

images-claude-code:  ## 建 Claude Code 的 Agent 镜像（要先有 $(GOLDEN_IMAGE)）
	docker build -t $(CLAUDE_CODE_IMAGE) images/claude-code

clean:               ## 清理缓存
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.mypy_cache $(BACKEND)/.ruff_cache
