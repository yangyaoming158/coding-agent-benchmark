"""MiniAgent 四工具、循环预算和记账的确定性测试，不调用外部模型。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.runner.miniagent_runtime import FileTools, run_loop


def reply(*calls: tuple[str, dict[str, str]], cached: int = 5) -> dict[str, Any]:
    return {
        "model": "test-model",
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "prompt_cache_hit_tokens": cached},
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": str(n),
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                        for n, (name, args) in enumerate(calls)
                    ],
                }
            }
        ],
    }


def options(**overrides: Any) -> dict[str, Any]:
    return {
        "prompt": "fix the issue",
        "model": "test-model",
        "temperature": 0,
        "max_turns": 5,
        "max_output_tokens": 100,
        "deadline_unix_ms": 10_000,
        "max_tokens_budget": 100_000,
        **overrides,
    }


def test_read_search_edit_and_create(tmp_path: Path) -> None:
    tools = FileTools(tmp_path)
    (tmp_path / "a.py").write_text("first\nbug\n")
    assert tools.call("list_dir", {"path": "."}) == "a.py"
    assert tools.call("read_file", {"path": "a.py", "start_line": "2"}) == "2: bug"
    assert "a.py:2: bug" in tools.call("grep", {"path": ".", "text": "bug"})
    assert (
        tools.call("apply_edit", {"path": "a.py", "old_text": "bug", "new_text": "fixed"})
        == "Edit applied."
    )
    assert (tmp_path / "a.py").read_text() == "first\nfixed\n"
    assert (
        tools.call("apply_edit", {"path": "new/b.py", "old_text": "", "new_text": "created"})
        == "Edit applied."
    )


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", ".git/config", "x/../../secret"])
def test_paths_cannot_escape_or_access_history(tmp_path: Path, path: str) -> None:
    assert "tool error" in FileTools(tmp_path).call("read_file", {"path": path})


def test_symlinks_and_ambiguous_edits_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "link").symlink_to("/etc")
    (tmp_path / "a").write_text("same same")
    tools = FileTools(tmp_path)
    assert "tool error" in tools.call("read_file", {"path": "link/passwd"})
    assert "tool error" in tools.call(
        "apply_edit", {"path": "a", "old_text": "same", "new_text": "no"}
    )
    assert "tool error" in tools.call("apply_edit", {"path": "a", "old_text": "", "new_text": "no"})
    assert (tmp_path / "a").read_text() == "same same"


def test_grep_skips_git_and_binary_and_never_follows_links(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "secret").write_text("needle")
    (tmp_path / "binary").write_bytes(b"needle\x00")
    assert "No matches" in FileTools(tmp_path).call("grep", {"path": ".", "text": "needle"})


def test_loop_passes_tool_results_back_and_accounts_each_turn(tmp_path: Path) -> None:
    events: list[dict[str, Any]] = []
    (tmp_path / "code.py").write_text("bug")
    replies = [
        reply(("read_file", {"path": "code.py", "start_line": "1"})),
        reply(("apply_edit", {"path": "code.py", "old_text": "bug", "new_text": "fix"})),
        reply(),
    ]
    requests = []

    def request(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        requests.append(json.loads(json.dumps(payload)))
        assert 0 < timeout <= 10
        return replies.pop(0)

    assert run_loop(options(), tmp_path, request, events.append, lambda: 0) == "finished"
    assert requests[1]["messages"][-1]["content"] == "1: bug"
    assert (tmp_path / "code.py").read_text() == "fix"
    assert sum(e["total"] for e in events if e["type"] == "llm_usage") == 36
    assert sum(e["cache_read"] for e in events if e["type"] == "llm_usage") == 15


@pytest.mark.parametrize(
    "changes,reason",
    [({"deadline_unix_ms": 1}, "deadline"), ({"max_tokens_budget": 1}, "token_budget")],
)
def test_no_call_after_budget_or_deadline(
    tmp_path: Path, changes: dict[str, Any], reason: str
) -> None:
    def forbidden(*args: Any) -> dict[str, Any]:
        pytest.fail("must not contact provider")

    assert run_loop(options(**changes), tmp_path, forbidden, lambda e: None, lambda: 2) == reason


def test_max_turns_and_bad_arguments_return_without_crashing(tmp_path: Path) -> None:
    response = reply(("read_file", {"path": "missing"}))
    response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{"
    events: list[dict[str, Any]] = []
    assert (
        run_loop(options(max_turns=2), tmp_path, lambda p, t: response, events.append, lambda: 0)
        == "max_turns"
    )
    assert len([e for e in events if e["type"] == "llm_usage"]) == 2
    assert all("tool error" in e["result"] for e in events if e["type"] == "tool_call")


def test_missing_usage_does_not_silently_report_zero(tmp_path: Path) -> None:
    response = reply()
    del response["usage"]
    with pytest.raises(ValueError, match="token usage"):
        run_loop(options(), tmp_path, lambda p, t: response, lambda e: None, lambda: 0)


def test_budget_reserves_next_request_and_limits_output(tmp_path: Path) -> None:
    requests = []

    def request(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        requests.append(payload)
        assert payload["thinking"] == {"type": "disabled"}
        assert 1 <= payload["max_tokens"] <= 100
        response = reply(("list_dir", {"path": "."}))
        response["usage"]["prompt_tokens"] = 9500
        return response

    reason = run_loop(
        options(max_tokens_budget=10_000, thinking="disabled"),
        tmp_path,
        request,
        lambda e: None,
        lambda: 0,
    )
    assert reason == "token_budget" and len(requests) == 1


def test_deadline_after_response_prevents_edit(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("old")
    now = [0.0]

    def request(p: dict[str, Any], t: float) -> dict[str, Any]:
        now[0] = 11
        return reply(("apply_edit", {"path": "a", "old_text": "old", "new_text": "new"}))

    assert run_loop(options(), tmp_path, request, lambda e: None, lambda: now[0]) == "deadline"
    assert (tmp_path / "a").read_text() == "old"
