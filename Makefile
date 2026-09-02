# 常用命令。所有 Python 命令都通过 uv 跑，不需要手动激活虚拟环境。
.PHONY: help install lint format type imports test test-all check env clean

BACKEND := backend
UV := cd $(BACKEND) && uv run

help:                ## 显示这份帮助
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:             ## 安装依赖并装好提交钩子
	cd $(BACKEND) && uv sync
	$(UV) pre-commit install
	$(UV) pre-commit install --hook-type commit-msg

lint:                ## 代码检查
	$(UV) ruff check .

format:              ## 自动格式化
	$(UV) ruff format .
	$(UV) ruff check --fix .

type:                ## 类型检查
	$(UV) mypy app cli

imports:             ## 模块边界检查（§14.2 的依赖方向）
	$(UV) lint-imports

test:                ## 快速测试（不含需要 Docker 和真实大模型的）
	$(UV) pytest -m "not docker and not agent"

test-all:            ## 全部测试
	$(UV) pytest

check: lint type imports test   ## 提交前跑一遍：检查 + 类型 + 边界 + 测试

env:                 ## 开发环境自检
	python3 scripts/check_env.py

report:              ## 重新生成规划报告 HTML
	python3 docs/plan/_build_report.py .

clean:               ## 清理缓存
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.mypy_cache $(BACKEND)/.ruff_cache
