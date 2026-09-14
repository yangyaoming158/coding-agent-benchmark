"""Stage2 结构化特征提取（E6-T1，`06-judge-attribution.md` §12.2 第二步）。

一句话：**把"大模型要看什么"先确定性地算出来，它自己不用再从原始日志里翻。**

§12.2 列了四组特征，这里一组一个纯函数：

| 特征 | 函数 | 回答的问题 |
|:---|:---|:---|
| 改的文件和官方补丁重合多少 | `patch_overlap()` | 找对地方了吗（F2 vs F3/F4） |
| 报错信息修改前后变没变 | `message_shift()` | 代码路径真的走了新逻辑吗（F4 vs F1） |
| 日志里是什么错误类型 | `log_errors()` | 是不是压根没跑起来（F5） |
| 轨迹统计 | `trajectory_stats()` | 是不是在工具上打转（F8 的佐证） |

## 全部不调大模型

这一步的价值就在于**确定性**。特征算错了能查，大模型编的看不出来。
E6-T2 拿到的是这四组数 + 裁剪过的原文，不是一堆生日志。

## 取不到就标 unavailable，不编

四组特征各有各的前提：没有官方补丁就算不了重合度，没有 Noop 哨兵跑过
就没有"修改前"的报错。取不到时把维度名和原因写进 `unavailable`，
**不要用零值糊过去** —— `jaccard = 0.0` 的意思是"一个文件都没蒙对"，
和"没法算"差着十万八千里，混起来会让 E6-T2 直接判 F2。

## "修改前的报错"从哪来：Noop 哨兵

§12.2 要"报错信息修改前后是不是变了"，可是"修改前"的报错文本哪都没存 ——
验证证据 `evidence.json.gz` 的 `baseline` 只存了每条用例的**状态**，没存文本。

现成的来源是 **Noop 哨兵**：空补丁跑同一份数据集快照，它的 `test_results`
里那些报错就是"什么都不改时的报错"。实测 `benchmark-dev@v1` 的 Noop 运行
留下 85 条 `message_excerpt`，正好对上这份数据集 85 条 F2P。

所以这一维不需要改验证流水线。代价是**数据集没跑过 Noop 就取不到** ——
那时标 unavailable。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: 日志里出现这些，基本可以断定测试压根没跑起来（§12.1 的 F5）。
#: 键是归一化后的类型名，值是在日志里怎么认它。
_ERROR_PATTERNS: dict[str, re.Pattern[str]] = {
    "syntax": re.compile(r"\b(SyntaxError|IndentationError|TabError)\b"),
    "import": re.compile(r"\b(ImportError|ModuleNotFoundError)\b"),
    "collection": re.compile(r"(ERROR collecting|errors? during collection|INTERNALERROR)"),
    "attribute": re.compile(r"\bAttributeError\b"),
    "type": re.compile(r"\bTypeError\b"),
    "name": re.compile(r"\bNameError\b"),
    "assertion": re.compile(r"\bAssertionError\b"),
}

#: 轨迹摘要只留最后这么多次工具调用（§12.3 给大模型看的就是这一段）。
_TRAJECTORY_TAIL = 10


@dataclass(frozen=True)
class PatchOverlap:
    """AI 改的文件和官方补丁改的文件，重合到什么程度。"""

    agent_paths: list[str]
    gold_paths: list[str]
    #: 两边都改了的文件。
    shared: list[str]
    #: 交集 / 并集。两边都空时是 0.0，但那种情况根本不该走到这里。
    jaccard: float
    #: 有没有蒙对至少一个文件。F2（找错文件）的直接判据就是这个为假。
    hit_any: bool


@dataclass(frozen=True)
class MessageShift:
    """同一条 F2P 用例，现在的报错和"什么都不改"时的报错一不一样。"""

    #: 两边都有报错文本、能对比的用例数。
    compared: int
    #: 报错文本变了的条数 —— 说明代码路径确实走了新逻辑（F4 的佐证）。
    changed: int
    #: 报错文本一字没变的条数 —— 说明改动压根没生效（F1/F2 的佐证）。
    unchanged: int
    #: 基线里有、现在没有报错的用例（这条被修好了）。
    fixed: list[str]


@dataclass(frozen=True)
class LogErrors:
    """测试日志里出现了哪几类错误，各几次。"""

    #: 类型 → 出现次数。只记出现过的。
    counts: dict[str, int]
    #: 有没有出现"压根跑不起来"那三类（语法/导入/收集）。F5 的判据。
    blocked_before_running: bool


@dataclass(frozen=True)
class TrajectoryStats:
    """AI 自己干活的过程：跑了多少轮、工具报错多少次。"""

    tool_calls: int
    tool_errors: int
    #: 工具报错占比。一次都没调用时是 0.0。
    tool_error_ratio: float
    llm_calls: int
    messages: int
    #: 最后 10 次工具调用，给 §12.3 的轨迹摘要用。
    tail: list[dict[str, Any]]


@dataclass(frozen=True)
class Stage2Features:
    """四组特征打成一包。取不到的维度在 `unavailable` 里说明原因。"""

    patch_overlap: PatchOverlap | None = None
    message_shift: MessageShift | None = None
    log_errors: LogErrors | None = None
    trajectory: TrajectoryStats | None = None
    #: 维度名 → 为什么取不到。**不要用零值代替**，理由见模块文档。
    unavailable: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转成能直接落 jsonb、也能直接喂给 E6-T2 的字典。"""
        out: dict[str, Any] = {}
        if self.patch_overlap is not None:
            out["patch_overlap"] = {
                "agent_paths": self.patch_overlap.agent_paths,
                "gold_paths": self.patch_overlap.gold_paths,
                "shared": self.patch_overlap.shared,
                "jaccard": self.patch_overlap.jaccard,
                "hit_any": self.patch_overlap.hit_any,
            }
        if self.message_shift is not None:
            out["message_shift"] = {
                "compared": self.message_shift.compared,
                "changed": self.message_shift.changed,
                "unchanged": self.message_shift.unchanged,
                "fixed": self.message_shift.fixed,
            }
        if self.log_errors is not None:
            out["log_errors"] = {
                "counts": self.log_errors.counts,
                "blocked_before_running": self.log_errors.blocked_before_running,
            }
        if self.trajectory is not None:
            out["trajectory"] = {
                "tool_calls": self.trajectory.tool_calls,
                "tool_errors": self.trajectory.tool_errors,
                "tool_error_ratio": self.trajectory.tool_error_ratio,
                "llm_calls": self.trajectory.llm_calls,
                "messages": self.trajectory.messages,
                "tail": self.trajectory.tail,
            }
        if self.unavailable:
            out["unavailable"] = dict(self.unavailable)
        return out


def patch_overlap(agent_paths: Sequence[str], gold_paths: Sequence[str]) -> PatchOverlap:
    """算两份补丁改动文件的重合度。

    路径要求调用方先用 `app.domain.patch_paths.derive_patch_paths()` 归一化过 ——
    `./src/a.py` 和 `src/a.py` 是同一个文件，字符串却不相等（`AGENTS.md` §5.5 同一个坑）。
    """
    agent = sorted(set(agent_paths))
    gold = sorted(set(gold_paths))
    shared = sorted(set(agent) & set(gold))
    union = set(agent) | set(gold)
    return PatchOverlap(
        agent_paths=agent,
        gold_paths=gold,
        shared=shared,
        jaccard=round(len(shared) / len(union), 4) if union else 0.0,
        hit_any=bool(shared),
    )


def message_shift(
    current: Mapping[str, str | None], baseline: Mapping[str, str | None]
) -> MessageShift:
    """比对同一批用例"现在"和"什么都不改"时的报错文本。

    只比两边都有文本的用例。基线里有报错、现在没有的，算这条被修好了。
    """
    changed = 0
    unchanged = 0
    compared = 0
    fixed: list[str] = []
    for test_id, base_msg in baseline.items():
        if not base_msg:
            continue
        now_msg = current.get(test_id)
        if not now_msg:
            fixed.append(test_id)
            continue
        compared += 1
        if _normalize_message(now_msg) == _normalize_message(base_msg):
            unchanged += 1
        else:
            changed += 1
    return MessageShift(
        compared=compared, changed=changed, unchanged=unchanged, fixed=sorted(fixed)
    )


def log_errors(text: str) -> LogErrors:
    """数一遍日志里各类错误出现了多少次。"""
    counts = {name: len(pat.findall(text)) for name, pat in _ERROR_PATTERNS.items()}
    hit = {name: n for name, n in counts.items() if n}
    blocked = any(hit.get(name) for name in ("syntax", "import", "collection"))
    return LogErrors(counts=hit, blocked_before_running=bool(blocked))


def trajectory_stats(lines: Iterable[str]) -> TrajectoryStats:
    """从轨迹 JSONL 里数出轮数和工具报错。

    坏行直接跳过，不抛异常 —— 轨迹是诊断材料，一行写坏了不该让整次归因失败。
    """
    tool_calls = 0
    tool_errors = 0
    llm_calls = 0
    messages = 0
    tail: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "tool_call":
            tool_calls += 1
            # `ok` 缺省当成成功：老轨迹没有这个字段，缺省判失败会凭空造出一堆 F8。
            if event.get("ok") is False:
                tool_errors += 1
            tail.append(
                {
                    "name": event.get("name"),
                    "ok": event.get("ok"),
                    "summary": event.get("summary"),
                }
            )
        elif kind == "llm_usage":
            llm_calls += 1
        elif kind == "message":
            messages += 1
    return TrajectoryStats(
        tool_calls=tool_calls,
        tool_errors=tool_errors,
        tool_error_ratio=round(tool_errors / tool_calls, 4) if tool_calls else 0.0,
        llm_calls=llm_calls,
        messages=messages,
        tail=tail[-_TRAJECTORY_TAIL:],
    )


#: 报错文本里这些东西每次跑都不一样，比对之前要抹掉，否则条条都"变了"。
_VOLATILE = (
    (re.compile(r"0x[0-9a-fA-F]+"), "0xADDR"),  # 对象地址
    (re.compile(r"/tmp/[^\s'\"]+"), "/tmp/PATH"),  # 临时目录
    (re.compile(r"\b\d+\.\d+s\b"), "Ns"),  # 耗时
    (re.compile(r"\s+"), " "),  # 空白
)


def _normalize_message(message: str) -> str:
    """抹掉报错文本里每次都变的部分，再比对。"""
    out = message.strip()
    for pattern, repl in _VOLATILE:
        out = pattern.sub(repl, out)
    return out


__all__ = [
    "LogErrors",
    "MessageShift",
    "PatchOverlap",
    "Stage2Features",
    "TrajectoryStats",
    "log_errors",
    "message_shift",
    "patch_overlap",
    "trajectory_stats",
]
