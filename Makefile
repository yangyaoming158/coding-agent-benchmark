# 常用命令。所有 Python 命令都通过 uv 跑，不需要手动激活虚拟环境。
# 用 bash 不用 sh：dev 目标要用 trap 和 kill 0 一次收掉两个子进程。
SHELL := /bin/bash

.PHONY: help install lint format type imports test test-docker test-all check env clean \
        db-up db-down db-reset db-psql migrate migrate-down migrate-check seed \
        seed-tasks worker enqueue queue \
        dev dev-api dev-web web-install web-lint web-build gen-api report schema \
        golden golden-verify images images-aider test-agent

BACKEND := backend
FRONTEND := frontend
# Golden 题的测试镜像标签。测试执行器的默认值和这里对齐（app/evaluation/test_executor.py）。
GOLDEN_IMAGE := bench-golden:py311
# Aider 的 Agent 镜像。适配器里的默认值和这里对齐（app/runner/adapters/aider.py），
# 数据库里那份在 agent_configs.params["image"]
AIDER_IMAGE := bench-agent:py311-aider
UV := cd $(BACKEND) && uv run

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
AGENT ?= oracle
NAME ?= adhoc

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

test:                ## 快速测试（不含需要 Docker 和真实大模型的）
	$(UV) pytest -m "not docker and not agent"

# 要排掉 agent：那些用例会真的调大模型，是要花钱的。
# 夜间跑一次 test-docker 就把额度烧掉一截，而且没人会注意到
test-docker:         ## 只跑需要 Docker 的沙箱测试（不含花钱的）
	$(UV) pytest -m "docker and not agent"

test-agent:          ## 跑会真的调用大模型的用例（要 API Key，会花钱，手动触发）
	$(UV) pytest -m agent

test-all:            ## 全部测试
	$(UV) pytest

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

worker:              ## 起一个 Worker 进程（Ctrl-C 优雅停机）
	$(UV) python -m app.worker

enqueue:             ## 建一次实验并把库里的题全投进队列（AGENT=oracle NAME=adhoc）
	$(UV) python -m cli.queue enqueue --agent $(AGENT) --name $(NAME)

queue:               ## 看作业队列现状
	$(UV) python -m cli.queue status

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

images:              ## 建 Golden 题的测试镜像（E4-T2 用，E2-T3 到位后由构建器接管）
	docker build -t $(GOLDEN_IMAGE) images/golden

images-aider:        ## 建 Aider 的 Agent 镜像（要先有 $(GOLDEN_IMAGE)）
	docker build -t $(AIDER_IMAGE) images/aider

clean:               ## 清理缓存
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.mypy_cache $(BACKEND)/.ruff_cache
