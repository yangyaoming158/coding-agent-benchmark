"""MiniAgent 的 stdin/stdout 协议入口；任务工作区须已由平台物化。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.infrastructure.config import get_settings
from app.runner.adapters.miniagent import MiniAgentRunner
from app.runner.protocol import AgentConfig, AgentTaskInput
from app.sandbox.container import build_env
from app.sandbox.git_cli import run_git
from app.sandbox.workspace import Workspace


def main() -> None:
    """读取一行协议任务，最后一行输出协议结果；制品和结果可另存文件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--result-file", type=Path)
    parser.add_argument("--params-file", type=Path, help="适配器 JSON 配置，含预算和单价")
    args = parser.parse_args()
    task = AgentTaskInput.model_validate_json(sys.stdin.readline())
    path = Path(task.workspace_path).resolve()
    sha = run_git(["rev-parse", "HEAD"], cwd=path).stdout.strip()
    workspace = Workspace(
        path=path,
        base_commit=task.repo.base_commit,
        base_sha=sha,
        tree_sha=run_git(["rev-parse", "HEAD^{tree}"], cwd=path).stdout.strip(),
        file_count=0,
    )
    params = json.loads(args.params_file.read_text()) if args.params_file else {}
    runner = MiniAgentRunner(params=params)
    result = runner.run(
        task,
        workspace,
        AgentConfig(
            env=build_env(get_settings().agent_env_for(task.model.name)),
            artifact_dir=args.artifact_dir.resolve(),
        ),
    )
    text = result.model_dump_json()
    if args.result_file:
        args.result_file.parent.mkdir(parents=True, exist_ok=True)
        args.result_file.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
