"""LLM 质量预筛（E1-T5，`03-benchmark-spec.md` §8.4 第七、八步）。

一句话：**问模型"这条候选够不够格当一道题"，拿一个 0~5 分和几个布尔量回来。**

拼 prompt、解析回答、按分数分流，全是纯函数，可以不花钱测。真正调模型在
`cli/prescreen.py`，客户端在 `app.infrastructure.llm`。

## 模型只用来"分析"，绝不用来判定

`AGENTS.md` §5.1 那条铁律：**判定必须 100% 由测试结果推导。** 这里的分数
不参与任何解决率计算，它只决定"这条候选值不值得再往下走"——
走下去之后，题目立不立得住由 E1-T3 的八步验证说了算，那一步一个模型都不调。

换句话说，模型在这里的角色是**筛选器**，不是**裁判**。筛错了最坏的后果是
少一道题或者多花一次验证的机时；判错了才会让排行榜不可信。

## 问哪四件事

按 §8.4 和 §7.2(7) 的坏任务清单，四个各自独立的问题：

| 问题 | 对应的坏任务 |
|:---|:---|
| issue 自足吗？ | "描述过短/无信息（'不工作'）" |
| 泄题吗？ | "Issue 里直接给了修复代码/PR 链接" |
| 过度指定吗？ | "测试与代码耦合到只能猜出实现细节" |
| 能定位到要改哪儿吗？ | 题面和补丁对不上，多半是关联关系不准 |

拆成四个布尔量而不是只要一个总分：总分低的时候没人知道低在哪，
而这四个的处置方式完全不同 —— 泄题的要丢，过度指定的要标记，
描述短的进人工队列。

## 温度必须是 0

同一条候选今天打 4 分、下个月打 2 分的话，这个分数就没法当门槛用。
客户端默认就是 0，这里再说一遍是因为**它容易被"调高一点更有创造力"改掉**。

## Prompt 版本号

`PROMPT_VERSION` 进缓存 key。改了 prompt 但忘了升版本，重跑会读到旧回答，
而且不报错 —— 那会让"我改进了 prompt"这件事完全测不出来。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.domain.enums import IssueLanguage

#: Prompt 的版本。**改了 prompt 就要升**，它进缓存 key。
PROMPT_VERSION = "1.0"

#: 分数区间 → 怎么处置。§8.4 原文：
#: `score<2 丢弃；2≤score<4 → REVIEW_REQUIRED；≥4 → 直接进 VALIDATING`。
REJECT_BELOW = 2.0
REVIEW_BELOW = 4.0

#: 分数上下限。模型偶尔会给 7 分或 -1 分，夹住而不是报错 ——
#: 为一条越界的分数丢掉整批回答不值得，夹完仍然落在正确的档里。
SCORE_MIN = 0.0
SCORE_MAX = 5.0

#: issue 正文短于这个字数直接进人工复核（§7.2(7)、§7.9 的 `review_flags`）。
#: 和 `schema.MIN_ISSUE_BODY_CHARS` 保持一致，两处不能各写一个数。
MIN_ISSUE_BODY_CHARS = 200

#: 喂给模型的正文上限（字符）。超了从中间截断，头尾都留 ——
#: issue 的结论常常在最后一段，只留开头会把它切掉。
MAX_ISSUE_CHARS = 6000
#: 喂给模型的补丁上限。只给 code_patch（也就是官方修复），
#: 因为"题面能不能推出这个修复"正是要问的。
MAX_PATCH_CHARS = 4000

SYSTEM_PROMPT = """\
你是一个软件测试基准的质检员。你要判断一条从 GitHub 挖来的候选，
够不够格变成一道"给 AI 编程助手做的修 bug 题"。

一道好题长这样：issue 把问题说清楚了，一个没看过修复代码的工程师读完
知道该往哪儿查；而且修复方案不在题面里。

你只输出 JSON，不要有任何别的文字。"""

USER_PROMPT = """\
## 仓库
{repo}

## Issue 标题
{title}

## Issue 正文（已做过正则脱敏，`［...已移除］`是被拿掉的东西）
{body}

## 这个 PR 改了哪些源码文件
{code_paths}

## 官方修复补丁（仅供你判断"题面能不能推出它"，不要在回答里复述它）
```diff
{code_patch}
```

## 请回答这四个问题，输出 JSON

{{
  "self_contained": true/false,   // 只读 issue（不看补丁），能不能明白问题是什么、怎么复现
  "leaks_fix": true/false,        // issue 里是不是已经给出了修复方案（贴改法、指明改哪行）
  "over_specified": true/false,   // issue 是不是钉死了实现细节（某个报错文案、某个内部函数名）
  "locatable": true/false,        // 只读 issue，能不能定位到大概要改哪个模块
  "score": 0-5,                   // 综合打分，见下
  "reason": "一句话中文说明，40 字以内"
}}

打分标准：
- 5：issue 清楚、可复现、不泄题，读完就知道往哪儿查
- 4：清楚但要多想一步，或者背景略少
- 3：能看懂但信息不全，人得猜一点
- 2：描述含糊，或者轻微泄题
- 1：几乎没信息（"不工作"、"报错了"），或者明显泄题
- 0：完全不可用（空正文、纯截图、答案直接贴在里面）
"""


@dataclass(frozen=True, slots=True)
class PrescreenInput:
    """喂给模型的一条候选。**正文必须是脱敏之后的。**"""

    repo: str
    pr_number: int
    title: str
    body: str
    code_paths: tuple[str, ...]
    code_patch: str
    language: IssueLanguage = IssueLanguage.EN

    def messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": USER_PROMPT.format(
                    repo=self.repo,
                    title=self.title or "（无标题）",
                    body=_clip(self.body, MAX_ISSUE_CHARS) or "（正文为空）",
                    code_paths="\n".join(f"- {p}" for p in self.code_paths) or "（无）",
                    code_patch=_clip(self.code_patch, MAX_PATCH_CHARS) or "（无）",
                ),
            },
        ]

    def cache_salt(self) -> dict[str, Any]:
        """进缓存 key 的额外信息。Prompt 版本必须在里面，理由见模块文档。"""
        return {"prompt_version": PROMPT_VERSION, "repo": self.repo, "pr": self.pr_number}


def _clip(text: str, limit: int) -> str:
    """太长就从中间截断，头尾都留。

    只留开头是常见做法，但对 issue 不合适：结论、复现步骤、"我试了 X 也不行"
    这些常常在最后一段。掐掉尾巴会让模型看到一个残缺的问题描述，
    然后把"信息不全"归咎于 issue 本身。
    """
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n\n……（中间省略 {len(text) - limit} 字）……\n\n{text[-tail:]}"


@dataclass(frozen=True, slots=True)
class PrescreenVerdict:
    """模型的结论 + 我们自己按规则加的标记。"""

    score: float
    self_contained: bool
    leaks_fix: bool
    over_specified: bool
    locatable: bool
    reason: str
    #: 我们自己加的标记（正文太短之类），不是模型给的。
    flags: tuple[str, ...] = ()

    @property
    def decision(self) -> str:
        """`REJECT` / `REVIEW` / `PASS`。§8.4 的分流规则就在这里。

        **泄题一票否决**，不管分数多少：模型说"issue 里已经给了修复方案"
        而我们还把它当题目，那就是拿答案考人。这一条比分数硬，
        因为分数是连续量、可以擦边，泄不泄题是个是非题。
        """
        if self.leaks_fix:
            return "REJECT"
        if self.score < REJECT_BELOW:
            return "REJECT"
        if self.score < REVIEW_BELOW or self.flags:
            return "REVIEW"
        return "PASS"

    def to_json(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "self_contained": self.self_contained,
            "leaks_fix": self.leaks_fix,
            "over_specified": self.over_specified,
            "locatable": self.locatable,
            "reason": self.reason,
            "flags": list(self.flags),
            "decision": self.decision,
            "prompt_version": PROMPT_VERSION,
        }


def _as_bool(value: Any, default: bool = False) -> bool:
    """模型有时把布尔写成 `"true"` / `"yes"` / `1`。都认。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y", "1", "是"}
    return default


def parse_verdict(payload: Any, *, flags: tuple[str, ...] = ()) -> PrescreenVerdict:
    """模型返回的 JSON → `PrescreenVerdict`。

    对缺字段宽容、对分数缺失严格：

    - 四个布尔缺了就按**保守方向**兜底（`self_contained=False`、`locatable=False`），
      这样漏答只会让候选走向人工复核，不会让一条烂题直接通过。
    - `score` 缺了或不是数字就抛 `ValueError` —— 分数是分流的唯一依据，
      编一个默认值出来等于假装模型回答过。
    """
    if not isinstance(payload, dict):
        raise ValueError(f"模型没返回 JSON 对象，而是 {type(payload).__name__}")
    if "score" not in payload:
        raise ValueError(
            f"模型的回答里没有 score 字段：{json.dumps(payload, ensure_ascii=False)[:200]}"
        )
    try:
        score = float(payload["score"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"score 不是数字：{payload['score']!r}") from exc

    return PrescreenVerdict(
        score=min(SCORE_MAX, max(SCORE_MIN, score)),
        self_contained=_as_bool(payload.get("self_contained")),
        leaks_fix=_as_bool(payload.get("leaks_fix")),
        over_specified=_as_bool(payload.get("over_specified")),
        locatable=_as_bool(payload.get("locatable")),
        reason=str(payload.get("reason") or "").strip()[:200],
        flags=flags,
    )


def rule_flags(
    *, body: str, f2p_candidates: int, leaks: list[str] | None = None
) -> tuple[str, ...]:
    """不用问模型就能判的几条，对应 §7.9 的 `review_flags()`。

    这些**先于模型**算出来，理由是它们确定、免费、而且模型判不好：
    "正文有没有 200 字"是数出来的，问模型只会浪费 token 还可能数错。
    """
    flags: list[str] = []
    if len(body.strip()) < MIN_ISSUE_BODY_CHARS:
        flags.append(f"正文不足 {MIN_ISSUE_BODY_CHARS} 字")
    if f2p_candidates == 0:
        flags.append("抽不出候选 F2P")
    elif f2p_candidates > 20:
        flags.append(f"候选 F2P 有 {f2p_candidates} 条，超过 20")
    for leak in leaks or []:
        flags.append(f"脱敏后仍有{leak}")
    return tuple(flags)


def score_histogram(scores: list[float]) -> list[tuple[str, int]]:
    """分数直方图。AC 里"预筛分数分布合理"就看它。

    按整数分档而不是等宽分箱：模型给的就是 0~5 的整数，
    分箱只会把 3 和 4 混在一起，而它俩的处置完全不同（复核 vs 通过）。
    """
    buckets = dict.fromkeys(range(6), 0)
    for score in scores:
        buckets[int(min(SCORE_MAX, max(SCORE_MIN, score)))] += 1
    return [(f"{n} 分", buckets[n]) for n in range(6)]


__all__ = [
    "MAX_ISSUE_CHARS",
    "MAX_PATCH_CHARS",
    "MIN_ISSUE_BODY_CHARS",
    "PROMPT_VERSION",
    "REJECT_BELOW",
    "REVIEW_BELOW",
    "SCORE_MAX",
    "SCORE_MIN",
    "SYSTEM_PROMPT",
    "USER_PROMPT",
    "PrescreenInput",
    "PrescreenVerdict",
    "parse_verdict",
    "rule_flags",
    "score_histogram",
]
