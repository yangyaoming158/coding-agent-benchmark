"""被测 AI 适配器：统一协议 + Mock/Oracle/Noop/Aider/ClaudeCode/MiniAgent 等实现。

协议本体在 `app.runner.protocol`（E3-T1）：

    from app.runner import AgentTaskInput, AgentRunResult, read_result

    line = task_input.to_stdin_line()          # 喂给适配器的 stdin
    result = read_result(stdout, result_file=cfg.result_file)   # 读回结果

写一个新适配器要做三件事：

1. 实现 `AgentRunner`（`name` / `probe()` / `run()`）；要自带镜像层再实现
   `ImageBuildingRunner.build_image()`。
2. 让它通过契约测试套件——继承 `tests/contract/runner_contract.py` 的
   `AgentRunnerContract`，给一个 `runner` fixture 就行，六条用例自动跑。
3. 在 `AgentRunResult` 里如实报 token 和成本；报不出来就把 `cost_source` 标成
   `unavailable`，不要填 0（协议纪律 3）。

依赖规则：可依赖 sandbox / storage / infrastructure / domain。
"""

from app.runner.protocol import (
    FORBIDDEN_INPUT_KEYS,
    RUNNER_PROTOCOL_VERSION,
    AgentConfig,
    AgentError,
    AgentRunner,
    AgentRunResult,
    AgentTaskInput,
    Constraints,
    ImageBuildingRunner,
    IssueInput,
    LeakyInputError,
    ModelInput,
    ProbeResult,
    ProtocolError,
    RepoInput,
    ResultParseError,
    TokenUsage,
    assert_no_leak,
    parse_result_line,
    parse_result_stdout,
    read_result,
)

__all__ = [
    "FORBIDDEN_INPUT_KEYS",
    "RUNNER_PROTOCOL_VERSION",
    "AgentConfig",
    "AgentError",
    "AgentRunResult",
    "AgentRunner",
    "AgentTaskInput",
    "Constraints",
    "ImageBuildingRunner",
    "IssueInput",
    "LeakyInputError",
    "ModelInput",
    "ProbeResult",
    "ProtocolError",
    "RepoInput",
    "ResultParseError",
    "TokenUsage",
    "assert_no_leak",
    "parse_result_line",
    "parse_result_stdout",
    "read_result",
]
