"""MiniAgent 容器内程序：标准库 HTTP + 四个工具，stdout 原生 JSONL（E3-T6）。

本文件单独只读挂载进已有 Python 镜像，不挂平台源码、题库或制品目录。
模型只能通过工作区内的文件工具行动，不执行 shell，不读取 git 元数据。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: 工具读取与回传都有上限，避免一个大文件耗尽内存或上下文。
MAX_FILE_BYTES = 512_000
MAX_OUTPUT_CHARS = 16_000
MAX_SCAN_FILES = 2000


def tool_schema(name: str, description: str, fields: dict[str, str]) -> dict[str, Any]:
    """生成兼容 Chat Completions 的工具声明，所有参数显式给出。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    key: {"type": "string", "description": value} for key, value in fields.items()
                },
                "required": list(fields),
                "additionalProperties": False,
            },
        },
    }


TOOLS = [
    tool_schema(
        "list_dir",
        "List immediate children of a workspace directory.",
        {"path": "Relative directory, e.g. ."},
    ),
    tool_schema(
        "read_file",
        "Read a UTF-8 file, optionally starting at a 1-based line.",
        {"path": "Relative file path", "start_line": "1-based start line as a string"},
    ),
    tool_schema(
        "grep",
        "Search literal text recursively; return matching lines with paths.",
        {"path": "Relative file or directory", "text": "Literal text to find"},
    ),
    tool_schema(
        "apply_edit",
        "Replace exactly one occurrence of old_text; empty old_text creates a NEW file only.",
        {
            "path": "Relative file path",
            "old_text": "Exact old text",
            "new_text": "Replacement text",
        },
    ),
]


class FileTools:
    """限制到工作区的文件操作；路径越界、符号链接和 git 元数据均拒绝。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def path(self, raw: str) -> Path:
        """逐级检查链接，防止模型通过仓库里的链接读到容器环境或运行参数。"""
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
            raise ValueError("path must stay inside workspace and outside .git")
        path = self.root
        for part in relative.parts:
            path /= part
            if path.is_symlink():
                raise ValueError("symbolic links are not accessible")
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("path leaves workspace")
        return path

    def read(self, path: Path) -> str:
        """只读有界普通文件，拒绝目录、设备和二进制文件。"""
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("not a regular file or file exceeds 512 KB")
        data = path.read_bytes()
        if b"\x00" in data:
            raise ValueError("binary file")
        return data.decode("utf-8")

    def call(self, name: str, args: dict[str, Any]) -> str:
        """错误返回给模型修正；不会因为一次找不到字符串而让整个 Agent 崩溃。"""
        try:
            result = self._call(name, args)
            return result[:MAX_OUTPUT_CHARS] + (
                "\n[truncated]" if len(result) > MAX_OUTPUT_CHARS else ""
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return f"tool error: {exc}"

    def _call(self, name: str, args: dict[str, Any]) -> str:
        path = self.path(args["path"])
        if name == "list_dir":
            return "\n".join(
                p.name + ("/" if p.is_dir() else "")
                for p in sorted(path.iterdir())
                if p.name != ".git" and not p.is_symlink()
            )
        if name == "read_file":
            start = int(args.get("start_line", "1"))
            if start < 1:
                raise ValueError("start_line must be positive")
            return "\n".join(
                f"{n}: {line}"
                for n, line in enumerate(self.read(path).splitlines(), 1)
                if n >= start
            )
        if name == "grep":
            needle = args["text"]
            if not isinstance(needle, str) or not needle:
                raise ValueError("text must be nonempty")
            files = [path] if path.is_file() else self._files(path)
            matches: list[str] = []
            for candidate in files:
                try:
                    content = self.read(candidate)
                except (OSError, ValueError):
                    continue
                for n, line in enumerate(content.splitlines(), 1):
                    if needle in line:
                        matches.append(f"{candidate.relative_to(self.root)}:{n}: {line}")
                        if len(matches) >= 100:
                            return "\n".join(matches) + "\n[match limit]"
            return "\n".join(matches) or "No matches (scan limited to 2000 files)."
        if name == "apply_edit":
            old, new = args["old_text"], args["new_text"]
            if not isinstance(old, str) or not isinstance(new, str):
                raise ValueError("old_text and new_text must be strings")
            content = self.read(path) if path.exists() else ""
            if not old:
                if path.exists():
                    raise ValueError("empty old_text only creates a new file")
                updated = new
            else:
                if content.count(old) != 1:
                    raise ValueError("old_text must match exactly once; read the file first")
                updated = content.replace(old, new, 1)
            if len(updated.encode()) > MAX_FILE_BYTES:
                raise ValueError("edited file exceeds 512 KB")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(updated, encoding="utf-8")
            return "Edit applied."
        raise ValueError(f"unknown tool: {name}")

    def _files(self, path: Path) -> list[Path]:
        files: list[Path] = []
        for directory, dirs, names in os.walk(path, followlinks=False):
            dirs[:] = sorted(
                d for d in dirs if d != ".git" and not (Path(directory) / d).is_symlink()
            )
            for name in sorted(names):
                candidate = Path(directory) / name
                if name != ".git" and not candidate.is_symlink():
                    files.append(candidate)
                    if len(files) >= MAX_SCAN_FILES:
                        return files
        return files


def emit(event: dict[str, Any]) -> None:
    """每个事件立即刷新，容器超时被杀也保留之前的用量与工具证据。"""
    print(json.dumps({"ts": int(time.time() * 1000), **event}, ensure_ascii=False), flush=True)


def complete(payload: dict[str, Any], timeout: float, *, base_url: str, key: str) -> dict[str, Any]:
    """单次 HTTP 调用；不在这里重试，避免同一道题隐式多次采样或重复计费。"""
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result: dict[str, Any] = json.loads(response.read(4_000_000))
    return result


def run_loop(
    config: dict[str, Any],
    root: Path,
    request: Callable[[dict[str, Any], float], dict[str, Any]],
    write: Callable[[dict[str, Any]], None] = emit,
    clock: Callable[[], float] = time.time,
) -> str:
    """模型 → 工具 → 结果循环；轮次、token 和绝对截止时刻任一用完就停止。"""
    tools = FileTools(root)
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "Use the supplied file tools to repair the issue. "
                "Tool paths are relative to the workspace. "
                "When done, respond without tool calls. Do not seek external repository history."
            ),
        },
        {"role": "user", "content": config["prompt"]},
    ]
    used = 0
    reason = "max_turns"
    for turn in range(1, config["max_turns"] + 1):
        remaining = config["deadline_unix_ms"] / 1000 - clock()
        if remaining <= 0:
            reason = "deadline"
            break
        # 不装 tokenizer；请求文本 UTF-8 字节数作保守的输入预算预留，不当成实际用量。
        reserve = (
            len(json.dumps({"messages": messages, "tools": TOOLS}, ensure_ascii=False).encode())
            + 1024
        )
        budget = config.get("max_tokens_budget")
        available = (
            config["max_output_tokens"]
            if budget is None
            else min(config["max_output_tokens"], budget - used - reserve)
        )
        if available < 1:
            reason = "token_budget"
            break
        payload = {
            "model": config["model"],
            "messages": messages,
            "tools": TOOLS,
            "temperature": config["temperature"],
            "max_tokens": available,
            "stream": False,
        }
        if config.get("thinking") is not None:
            payload["thinking"] = {"type": config["thinking"]}
        response = request(payload, min(remaining, 120.0))
        usage = response.get("usage")
        if not isinstance(usage, dict) or not all(
            isinstance(usage.get(k), int) and usage[k] >= 0
            for k in ("prompt_tokens", "completion_tokens")
        ):
            write({"type": "usage_missing", "turn": turn})
            raise ValueError("provider did not return valid token usage")
        input_tokens, output_tokens = usage["prompt_tokens"], usage["completion_tokens"]
        cached = usage.get(
            "prompt_cache_hit_tokens",
            (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
        )
        if not isinstance(cached, int) or not 0 <= cached <= input_tokens:
            raise ValueError("invalid cache token count")
        used += input_tokens + output_tokens
        write(
            {
                "type": "llm_usage",
                "turn": turn,
                "input": input_tokens,
                "output": output_tokens,
                "cache_read": cached,
                "total": input_tokens + output_tokens,
                "model": response.get("model", config["model"]),
            }
        )
        message = response["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        # 只保留接口接受的字段；部分兼容端点要求回传 reasoning_content。
        messages.append(
            {
                k: message[k]
                for k in ("role", "content", "tool_calls", "reasoning_content")
                if k in message
            }
        )
        write(
            {
                "type": "message",
                "role": "assistant",
                "text_excerpt": (message.get("content") or "")[:2000],
                "turn": turn,
            }
        )
        if clock() * 1000 >= config["deadline_unix_ms"]:
            reason = "deadline"
            break
        if budget is not None and used >= budget:
            reason = "token_budget"
            break
        if not calls:
            reason = "finished"
            break
        for call in calls:
            if clock() * 1000 >= config["deadline_unix_ms"]:
                reason = "deadline"
                break
            function = call["function"]
            name = function["name"]
            try:
                args = json.loads(function["arguments"])
                if not isinstance(args, dict):
                    raise ValueError("arguments must be an object")
                result = tools.call(name, args)
            except (ValueError, TypeError) as exc:
                result = f"tool error: {exc}"
            write(
                {
                    "type": "tool_call",
                    "turn": turn,
                    "name": name,
                    "arguments": function["arguments"],
                    "result": result,
                }
            )
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        if reason == "deadline":
            break
    write({"type": "stop", "reason": reason})
    return reason


def main() -> int:
    """仅输出结构化错误，不把 API Key、HTTP 响应体或请求头写进日志。"""
    config = json.loads(sys.argv[1])
    key = os.environ.get(config["key_env"], "")
    if not key:
        emit({"type": "error", "text": "API Error: 401 missing API key"})
        return 1
    try:
        run_loop(
            config,
            Path.cwd(),
            lambda payload, timeout: complete(
                payload, timeout, base_url=config["base_url"], key=key
            ),
        )
    except urllib.error.HTTPError as exc:
        emit({"type": "error", "text": f"API Error: {exc.code}"})
        return 1
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        emit({"type": "error", "text": "API Error: 503 connection failed"})
        return 1
    except Exception as exc:
        emit({"type": "error", "text": f"MiniAgent runtime error: {type(exc).__name__}"})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
