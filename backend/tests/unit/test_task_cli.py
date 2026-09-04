"""题目导入/导出 CLI 的测试（E1-T1 的 Output）。

最要紧的一条是 `test_committed_schema_is_up_to_date`：`schemas/task.schema.json`
是**生成物**，改了模型忘了重新导出，对外的数据契约就和实际行为对不上了。
让它在 CI 里红，比让别人照着过期的 schema 写导入脚本便宜得多。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cli.task import DEFAULT_SCHEMA_PATH, main, render_schema
from tests.unit.test_task_schema import load_fixture, make_task

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "sample_task.json"


def write_task(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# ── JSON Schema ────────────────────────────────────────────


def test_committed_schema_is_up_to_date() -> None:
    """仓库里的 schemas/task.schema.json 必须和 TaskDefinition 一致。

    它是生成物，改了模型要跑 `make schema` 重新导出。
    """
    assert DEFAULT_SCHEMA_PATH.exists(), "schemas/task.schema.json 不见了，跑 make schema"
    assert DEFAULT_SCHEMA_PATH.read_text(encoding="utf-8") == render_schema(), (
        "schemas/task.schema.json 和模型对不上了，跑 make schema 重新导出"
    )


def test_schema_check_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["schema", "--check"]) == 0
    assert "最新" in capsys.readouterr().out


def test_schema_check_detects_drift(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """磁盘上的 schema 过期时要非零退出，不能静默通过。"""
    stale = tmp_path / "task.schema.json"
    stale.write_text('{"title": "过期的"}', encoding="utf-8")
    assert main(["schema", "--check", "--out", str(stale)]) == 1
    assert "对不上" in capsys.readouterr().err


def test_schema_export_writes_file(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "task.schema.json"
    assert main(["schema", "--out", str(out)]) == 0
    schema = json.loads(out.read_text(encoding="utf-8"))
    assert "task_id" in schema["properties"]
    assert "fail_to_pass" in schema["required"]


def test_schema_render_is_stable() -> None:
    """连续导出两次内容一致——否则每次跑都产生一份"有改动"的文件，diff 全是噪声。"""
    assert render_schema() == render_schema()


# ── import ─────────────────────────────────────────────────


def test_import_accepts_golden_task(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["import", str(FIXTURE)]) == 0
    out = capsys.readouterr().out
    assert "nonebot__nonebot2-2314" in out
    assert "通过 1，拒收 0" in out


def test_import_rejects_bad_task_with_readable_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """坏题要非零退出，并且说清楚是哪个字段、哪条规则。

    "校验失败"三个字帮不上忙——拿到一批题的人需要知道改哪里。
    """
    bad = write_task(tmp_path / "bad.json", make_task(base_commit="3f2a1c9"))
    assert main(["import", str(bad)]) == 1
    err = capsys.readouterr().err
    assert "base_commit" in err
    assert "40 位" in err


def test_import_reports_review_flags(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """合法但可疑的题要放行并标出来，不能当成坏题拒掉（§7.4）。"""
    thin = write_task(tmp_path / "thin.json", make_task(issue_body="重连之后重复了。"))
    assert main(["import", str(thin)]) == 0
    assert "需人工复核" in capsys.readouterr().out


def test_import_reads_jsonl_and_reports_line_number(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """数据集是 jsonl，坏题要报出行号——一个几百行的文件里没行号就只能一行行数。"""
    good = json.dumps(make_task(), ensure_ascii=False)
    bad = json.dumps(make_task(fail_to_pass=[]), ensure_ascii=False)
    path = tmp_path / "tasks.jsonl"
    path.write_text(f"{good}\n{bad}\n", encoding="utf-8")

    assert main(["import", str(path)]) == 1
    captured = capsys.readouterr()
    assert "tasks.jsonl:2" in captured.err
    assert "共 2 道，通过 1，拒收 1" in captured.out


# ── export ─────────────────────────────────────────────────


def test_export_backfills_content_hash(tmp_path: Path) -> None:
    """手写的题没填哈希，导出时补上——这是"给手写题目补齐哈希"的正规做法。"""
    source = write_task(tmp_path / "in.json", make_task())
    out = tmp_path / "out.json"
    assert main(["export", str(source), "--out", str(out)]) == 0

    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["content_hash"] == load_fixture()["content_hash"]


def test_export_normalizes_collections(tmp_path: Path) -> None:
    """导出会把集合类字段排好序、把 test_patch_paths 重算出来。"""
    source = write_task(
        tmp_path / "in.json",
        make_task(tags=["zebra", "alpha"], test_patch_paths=[]),
    )
    out = tmp_path / "out.json"
    main(["export", str(source), "--out", str(out)])

    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["tags"] == ["alpha", "zebra"]
    assert result["test_patch_paths"] == ["tests/test_adapter.py"]


def test_export_to_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = write_task(tmp_path / "in.json", make_task())
    assert main(["export", str(source)]) == 0
    assert json.loads(capsys.readouterr().out)["task_id"] == "nonebot__nonebot2-2314"
