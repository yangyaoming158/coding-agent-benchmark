"""`git` 命令行的薄封装。

为什么直接调命令行，不用 GitPython / pygit2：这个模块只需要 `clone --mirror`、
`archive`、`init`、`add`、`commit`、`ls-tree` 这几条命令，而 `git archive` 的
输出必须逐字节可复现（协议 C-43 的物化方案就建立在这上面）。命令行是官方行为的
唯一权威，库封装多一层就多一层"它到底做了什么"的不确定。

## 一次干净的 git 调用长什么样

`run_git()` 做了三件容易被忽略的事：

1. **屏蔽用户的全局 git 配置**（`GIT_CONFIG_GLOBAL` / `GIT_CONFIG_SYSTEM` 指向
   `/dev/null`）。开发机上一句 `core.autocrlf=true` 就会让 `git add` 改写换行符，
   同一个 commit 在两台机器上物化出不同的目录树哈希 —— 而"两次物化哈希一致"正是
   E2-T1 的验收标准。`commit.gpgsign=true` 更直接：签名失败，物化整个报错。
2. **禁掉交互**（`GIT_TERMINAL_PROMPT=0`）。私有仓库或者 URL 打错时，git 默认会
   停下来问用户名密码。评测是无人值守跑的，那就是一直挂到超时。
3. **固定时区与 locale**（`TZ=UTC`、`LC_ALL=C.UTF-8`），让提交时间和报错文本稳定。

注意只覆盖这几个变量，**不清空整个环境**：`HTTP_PROXY` / `HTTPS_PROXY` 要原样传下去，
这台开发机上的 git 出网靠它们（见 AGENTS.md 第 10 节）。

如果你原本靠 `~/.gitconfig` 里的 `http.proxy` 出网，改成设 `HTTPS_PROXY` 环境变量 ——
全局配置在这里是被屏蔽的。
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: 单条 git 命令的默认超时（秒）。够大仓库 clone 用，又不至于让卡死的命令挂一整夜。
DEFAULT_GIT_TIMEOUT_S = 1800

#: 强制覆盖的环境变量。理由见模块文档。
_HERMETIC_ENV: dict[str, str] = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "TZ": "UTC",
    "LC_ALL": "C.UTF-8",
}


class GitError(RuntimeError):
    """git 命令以非零码退出，或者超时。

    错误消息里带上命令、退出码和 stderr 的尾部 —— 排查 git 问题九成靠这三样，
    只说"git failed"等于让人重跑一遍手工复现。
    """

    def __init__(self, args: Sequence[str], returncode: int, stderr: str) -> None:
        self.args_used = list(args)
        self.returncode = returncode
        self.stderr = stderr
        detail = stderr.strip().splitlines()
        tail = "\n".join(detail[-5:]) if detail else "（无 stderr 输出）"
        super().__init__(f"git {' '.join(args)} 失败（退出码 {returncode}）：\n{tail}")


@dataclass(frozen=True, slots=True)
class GitResult:
    """一条 git 命令的结果。`stdout` 已按 UTF-8 解码并去掉尾部换行。"""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def hermetic_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """当前进程的环境 + 强制覆盖的那几个 git 变量。

    直接跑 `git` 子进程（比如 `git archive` 要用 Popen 接管道）的地方也要用它，
    否则那条命令又会读回用户的全局配置，前面的功夫白做。
    """
    return {**os.environ, **_HERMETIC_ENV, **(extra or {})}


def run_git(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout_s: int = DEFAULT_GIT_TIMEOUT_S,
    check: bool = True,
    env_extra: Mapping[str, str] | None = None,
) -> GitResult:
    """跑一条 git 命令。

    `check=True`（默认）时非零退出抛 `GitError`；`check=False` 时把结果原样返回，
    留给调用方按退出码分支 —— `git cat-file -e <sha>` 这种"用退出码回答是非题"的
    命令要靠它。

    stdout 按 UTF-8 解码，非法字节用 `replace` 兜住：仓库里的文件名可能是任意字节，
    解码报错会让整条命令失败，而我们要的只是把文件名打进日志。
    """
    command = ["git", *args]
    env = hermetic_env(env_extra)
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            timeout=timeout_s,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(command, -1, f"超过 {timeout_s} 秒未结束（{exc}）") from exc
    except OSError as exc:  # git 没装、cwd 不存在
        raise GitError(command, -1, str(exc)) from exc

    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    if check and completed.returncode != 0:
        raise GitError(command, completed.returncode, stderr)
    return GitResult(tuple(command), completed.returncode, stdout.rstrip("\n"), stderr)


def git_stdout(args: Sequence[str], *, cwd: Path | None = None, timeout_s: int = 60) -> str:
    """跑一条只读命令并返回 stdout。默认超时短 —— 这类命令都是纯本地的。"""
    return run_git(args, cwd=cwd, timeout_s=timeout_s).stdout


__all__ = [
    "DEFAULT_GIT_TIMEOUT_S",
    "GitError",
    "GitResult",
    "git_stdout",
    "hermetic_env",
    "run_git",
]
