"""`python -m cli.runner` 的行为（E3-T1）。

最要紧的一条是 `test_committed_schemas_are_up_to_date`：`schemas/` 下那两份文件是
**对外的数据契约**，写适配器的人不一定用 Python，他们照着 schema 实现。
模型改了却忘了重新导出，外面的人就在照着一份过期的契约写代码，而且没人会发现。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli.runner import (
    INPUT_SCHEMA_PATH,
    RESULT_SCHEMA_PATH,
    main,
    render_schema,
    schema_targets,
)
from tests.unit.test_runner_protocol import make_input, make_result

SCHEMA_PATHS = [INPUT_SCHEMA_PATH, RESULT_SCHEMA_PATH]


def test_committed_schemas_are_up_to_date() -> None:
    """仓库里的两份 schema 必须和模型一致，对不上就跑 `make schema`。"""
    for path, rendered in schema_targets():
        assert path.exists(), f"{path.name} 不见了，跑 make schema"
        assert path.read_text(encoding="utf-8") == rendered, (
            f"{path.name} 和模型对不上了，跑 make schema 重新导出"
        )


def test_schema_check_passes_on_the_committed_files() -> None:
    assert main(["schema", "--check"]) == 0


def test_schema_render_is_stable() -> None:
    """两次导出必须逐字节相同。

    不稳定的话，每次跑 `make schema` 都产生一份"有改动"的文件，
    diff 里全是键序噪声，漂移检查也就失去意义了。
    """
    from app.runner.protocol import AgentTaskInput

    assert render_schema(AgentTaskInput) == render_schema(AgentTaskInput)


@pytest.mark.parametrize("path", SCHEMA_PATHS)
def test_schema_is_valid_json_with_a_title(path: Path) -> None:
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert schema["title"]
    assert schema["type"] == "object"


def test_task_input_schema_has_no_forbidden_property() -> None:
    """导出的契约里不能出现禁发字段。

    这条盯的是"schema 泄题"：别人照着 schema 写适配器，schema 里有 `fail_to_pass`，
    他就会去读它——哪怕我们的模型根本不发这个字段。
    """
    from app.runner.protocol import FORBIDDEN_INPUT_KEYS

    schema = json.loads(INPUT_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert not set(schema["properties"]) & FORBIDDEN_INPUT_KEYS


def test_validate_input_accepts_a_good_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "task.json"
    path.write_text(make_input().to_stdin_line(), encoding="utf-8")
    assert main(["validate-input", str(path)]) == 0
    assert "合法" in capsys.readouterr().out


def test_validate_input_rejects_a_leaky_file(tmp_path: Path) -> None:
    """人工造调试用的任务输入时，顺手把 `fail_to_pass` 粘进去是很容易发生的事。"""
    payload = json.loads(make_input().to_stdin_line())
    payload["fail_to_pass"] = ["tests/test_a.py::test_x"]
    path = tmp_path / "leaky.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert main(["validate-input", str(path)]) == 1


@pytest.mark.parametrize("content", ["{坏了", '["不是对象"]'])
def test_validate_input_rejects_garbage(tmp_path: Path, content: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(content, encoding="utf-8")
    assert main(["validate-input", str(path)]) == 1


def test_validate_result_reads_the_last_line_of_a_noisy_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """收的是整段 stdout，不是那一行 JSON。

    排查"结果读不出来"时，手边有的正是这一坨刷屏日志，
    让人先自己把最后一行找出来是多余的一步。
    """
    log = tmp_path / "agent.log"
    noise = "\n".join(f"[info] 第 {i} 步" for i in range(50))
    result_line = json.dumps(
        json.loads(make_result(patch="diff --git a/x b/x\n").model_dump_json())
    )
    log.write_text(f"{noise}\n{result_line}\n", encoding="utf-8")

    assert main(["validate-result", str(log)]) == 0
    assert "合法" in capsys.readouterr().out


def test_validate_result_reports_unreadable_output(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    log.write_text("[info] 干完了，但是忘了打印结果\n", encoding="utf-8")
    assert main(["validate-result", str(log)]) == 1


def test_schema_check_fails_when_a_file_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把落盘的 schema 改坏，`--check` 必须非零退出。

    没有这条的话，一个永远返回 0 的 `--check` 也能让 CI 全绿。
    """
    import cli.runner as cli_runner

    stale = tmp_path / "agent-task-input.schema.json"
    stale.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(cli_runner, "INPUT_SCHEMA_PATH", stale)
    assert main(["schema", "--check"]) == 1
