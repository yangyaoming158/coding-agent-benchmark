"""LLM-as-Judge 失败归因（E6-T2）。

这里的“Judge”只判断**为什么失败**，不判断 bug 是否修好。
`agent_outcome` 由测试判定引擎产生，本模块既不读写它，也不提供修改它的接口。

大模型只处理规则分不出的 F1～F5。输出用 Pydantic 生成的 JSON Schema
约束，并在本地再做一次严格校验。每条 evidence 必须逐字出现在它声称的
输入段里；引用对不上就重试，不把模型编的证据当成事实。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.domain.enums import AttributionStatus, FailureCategory
from app.infrastructure.llm import LLMError, LLMResponse

PROMPT_VERSION = "1.0"
LOW_CONFIDENCE = 0.6
VOTE_COUNT = 3
VALIDATION_ATTEMPTS = 3

MAX_ISSUE_CHARS = 3000
MAX_PATCH_CHARS = 6000
MAX_TEST_MESSAGE_CHARS = 2000

EvidenceSource = Literal["issue", "patch", "gold_summary", "test_log", "features", "trajectory"]
LLMCategory = Literal[
    FailureCategory.F1_REQUIREMENT_MISUNDERSTANDING,
    FailureCategory.F2_WRONG_FILE_LOCALIZATION,
    FailureCategory.F3_INCOMPLETE_FIX,
    FailureCategory.F4_INCORRECT_LOGIC,
    FailureCategory.F5_SYNTAX_OR_BUILD_ERROR,
]

ALLOWED_CATEGORIES = frozenset(
    {
        FailureCategory.F1_REQUIREMENT_MISUNDERSTANDING,
        FailureCategory.F2_WRONG_FILE_LOCALIZATION,
        FailureCategory.F3_INCOMPLETE_FIX,
        FailureCategory.F4_INCORRECT_LOGIC,
        FailureCategory.F5_SYNTAX_OR_BUILD_ERROR,
    }
)


class EvidenceCitation(BaseModel):
    """模型引用的一条证据。`quote` 后面还会和原文做逐字比对。"""

    model_config = ConfigDict(extra="forbid")

    source: EvidenceSource
    quote: str = Field(min_length=1, max_length=500)


class AttributionVerdict(BaseModel):
    """大模型回答的严格结构。类别只允许 F1～F5。"""

    model_config = ConfigDict(extra="forbid")

    category: LLMCategory
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[EvidenceCitation] = Field(min_length=1, max_length=8)
    reasoning_zh: str = Field(min_length=1, max_length=2000)
    secondary_category: LLMCategory | None = None

    @model_validator(mode="after")
    def validate_categories(self) -> AttributionVerdict:
        """次要类别是候选解释，不能重复主类别。"""
        if self.secondary_category is not None and self.secondary_category is self.category:
            raise ValueError("次要类别不能和主类别相同")
        return self


@dataclass(frozen=True, slots=True)
class FailedTest:
    """一条失败用例的裁剪摘要。"""

    test_id: str
    status: str
    message: str


@dataclass(frozen=True, slots=True)
class AttributionInput:
    """一次 F1～F5 归因所需的全部输入。

    `gold_summary` 只能是官方补丁的文件清单与改动规模，不能包含代码。
    这个类根本没有 `gold_patch` 字段，让调用方无法误把答案塞进 prompt。
    """

    task_run_id: int
    task_id: str
    issue_title: str
    issue_body: str
    agent_patch: str
    gold_summary: str
    failed_tests: tuple[FailedTest, ...]
    features: Mapping[str, object]

    def sections(self) -> dict[str, str]:
        """生成 prompt 的六个可引用段，同时作为 evidence 校验的原文。"""
        features = dict(self.features)
        trajectory = features.pop("trajectory", None)
        title = self.issue_title or "（无标题）"
        body = _clip(self.issue_body, MAX_ISSUE_CHARS) or "（空）"
        issue = f"标题：{title}\n正文：{body}"
        tests = "\n\n".join(
            f"[{test.status}] {test.test_id}\n{_clip(test.message, MAX_TEST_MESSAGE_CHARS)}"
            for test in self.failed_tests[:3]
        )
        return {
            "issue": issue,
            "patch": _clip(self.agent_patch, MAX_PATCH_CHARS) or "（空）",
            "gold_summary": self.gold_summary or "（不可用）",
            "test_log": tests or "（没有可用的失败用例摘要）",
            "features": _json_text(features),
            "trajectory": _json_text(trajectory if trajectory is not None else "（不可用）"),
        }

    def messages(self) -> list[dict[str, str]]:
        """构造模型消息。Schema 和证据来源名都在 prompt 中明示。"""
        sections = self.sections()
        schema = json.dumps(
            AttributionVerdict.model_json_schema(), ensure_ascii=False, sort_keys=True
        )
        material = "\n\n".join(
            f"## {name}\n```text\n{text}\n```" for name, text in sections.items()
        )
        system = (
            "你是 AI 编程评测的失败原因分析员。你不得判断 bug 是否修好，"
            "只能在 F1～F5 中分类。只输出符合 JSON Schema 的 JSON 对象，不要加 Markdown。"
        )
        user = f"""\
## 任务
task_run_id={self.task_run_id}
task_id={self.task_id}

## 可用材料
{material}

## 分类边界
- F1_REQUIREMENT_MISUNDERSTANDING：解决的不是 issue 要求的问题。
- F2_WRONG_FILE_LOCALIZATION：找错文件，修改和官方改动文件无交集。
- F3_INCOMPLETE_FIX：方向正确，但只修了一部分。
- F4_INCORRECT_LOGIC：找对位置，但新逻辑仍然错。
- F5_SYNTAX_OR_BUILD_ERROR：语法、导入、编译或测试收集阶段已经失败。

evidence 至少一条。source 必须是上面六个段名之一，quote 必须从该段
逐字复制，不得改写或编造。不要推测未提供的官方补丁代码。

## JSON Schema
{schema}
"""
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def prompt_hash(self) -> str:
        """稳定的 prompt 指纹：版本和完整消息任一变化都会变。"""
        payload = {"version": PROMPT_VERSION, "messages": self.messages()}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class CompletionClient(Protocol):
    """E6-T2 需要的最小模型客户端契约，方便用离线假回答测试。"""

    model: str

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_mode: bool = False,
        cache_salt: Mapping[str, Any] | None = None,
        refresh: bool = False,
    ) -> LLMResponse: ...


@dataclass(frozen=True, slots=True)
class AttributionDecision:
    """可直接落库的自动归因结果。"""

    verdict: AttributionVerdict
    status: AttributionStatus
    judge_model: str
    prompt_hash: str
    raw_response: dict[str, object]


class AttributionRunError(LLMError):
    """多次回答都无法得到一个有类别、有证据的结果。"""


def parse_verdict(payload: object, *, sources: Mapping[str, str]) -> AttributionVerdict:
    """JSON 回答 → 严格结论，并校验每条证据真在对应输入里。"""
    try:
        verdict = AttributionVerdict.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"归因回答不符合 JSON Schema（类别只允许 F1～F5）：{exc}") from exc
    for citation in verdict.evidence:
        source = sources[citation.source]
        if citation.quote not in source:
            raise ValueError(f"evidence 不在 {citation.source} 原文中：{citation.quote[:80]!r}")
    return verdict


def result_cache_hit(
    existing_model: str | None,
    existing_prompt_hash: str | None,
    *,
    model: str,
    prompt_hash: str,
) -> bool:
    """同一运行的库内结果是否命中“提示词 + 模型”身份。

    `evaluation_task_run_id` 由查到这行时已经锁定，所以这里只比较三元组
    剩下的两项。任一变化都必须重算，否则换模型或改 prompt 会静默读旧结果。
    """
    return existing_model == model and existing_prompt_hash == prompt_hash


def attribute_failure(
    payload: AttributionInput,
    client: CompletionClient,
    *,
    refresh: bool = False,
) -> AttributionDecision:
    """运行一次 LLM 归因；低置信时取 3 票，无多数结论就转人工。

    每一票最多做 `VALIDATION_ATTEMPTS` 次结构/证据校验。HTTP 429 和 5xx
    的退避重试由更下层的 `LLMClient` 负责。两层分开，因为“回了 200
    但 JSON 是坏的”和“请求根本没成功”是两种不同故障。
    """
    messages = payload.messages()
    sources = payload.sections()
    prompt_hash = payload.prompt_hash()
    votes: list[AttributionVerdict] = []
    raw_votes: list[dict[str, object]] = []
    errors: list[str] = []

    first = _validated_vote(
        client,
        messages,
        sources,
        task_run_id=payload.task_run_id,
        prompt_hash=prompt_hash,
        vote_index=0,
        refresh=refresh,
        raw_votes=raw_votes,
        errors=errors,
    )
    if first is None:
        raise AttributionRunError("重试后仍没有可用的结构化归因：" + "; ".join(errors[-3:]))
    votes.append(first)

    if first.confidence >= LOW_CONFIDENCE:
        return AttributionDecision(
            verdict=first,
            status=AttributionStatus.OK,
            judge_model=client.model,
            prompt_hash=prompt_hash,
            raw_response={"votes": raw_votes, "validation_errors": errors},
        )

    for vote_index in range(1, VOTE_COUNT):
        vote = _validated_vote(
            client,
            messages,
            sources,
            task_run_id=payload.task_run_id,
            prompt_hash=prompt_hash,
            vote_index=vote_index,
            refresh=refresh,
            raw_votes=raw_votes,
            errors=errors,
        )
        if vote is not None:
            votes.append(vote)

    counts = Counter(vote.category for vote in votes)
    category, count = counts.most_common(1)[0]
    majority = count >= 2 and len(votes) == VOTE_COUNT
    chosen = max(
        (vote for vote in votes if vote.category is category), key=lambda vote: vote.confidence
    )
    return AttributionDecision(
        verdict=chosen,
        status=AttributionStatus.OK if majority else AttributionStatus.NEEDS_HUMAN,
        judge_model=client.model,
        prompt_hash=prompt_hash,
        raw_response={
            "votes": raw_votes,
            "validation_errors": errors,
            "vote_categories": [vote.category.value for vote in votes],
        },
    )


def _validated_vote(
    client: CompletionClient,
    messages: Sequence[Mapping[str, str]],
    sources: Mapping[str, str],
    *,
    task_run_id: int,
    prompt_hash: str,
    vote_index: int,
    refresh: bool,
    raw_votes: list[dict[str, object]],
    errors: list[str],
) -> AttributionVerdict | None:
    for validation_attempt in range(VALIDATION_ATTEMPTS):
        # temperature=0 时原样重问会再次得到同一份坏证据。第二、三次补一条
        # 明确的纠错指令；原始材料不变，证据仍须经下方逐字校验。
        retry_messages = list(messages)
        if validation_attempt == 1:
            retry_messages.append(
                {
                    "role": "user",
                    "content": (
                        "上次回答未通过结构或证据校验。只返回完整 JSON。"
                        "evidence.quote 必须从 source 段逐字复制连续原文，建议只引用一行。"
                        "这次不要引用 patch；请从 features 或 test_log 中"
                        "复制直接支持结论的短句，保留原有空格与标点。"
                    ),
                }
            )
        elif validation_attempt == 2:
            retry_messages.append(
                {
                    "role": "user",
                    "content": (
                        "前两次回答未通过校验。evidence.source 不得为 patch；"
                        "从 issue、features 或 test_log 复制一段不超过 80 字符的连续原文，"
                        "逐字保留空格、引号和标点。只返回完整 JSON，理由写一句话。"
                    ),
                }
            )
        try:
            response = client.complete(
                retry_messages,
                temperature=0.0,
                max_tokens=4096,
                json_mode=True,
                cache_salt={
                    "purpose": "failure_attribution",
                    "task_run_id": task_run_id,
                    "prompt_hash": prompt_hash,
                    "prompt_version": PROMPT_VERSION,
                    "vote_index": vote_index,
                    "validation_attempt": validation_attempt,
                },
                refresh=refresh,
            )
        # 客户端已经对 429 / 5xx 做了 3 次退避重试。这里再重试
        # 会把一次持续故障放大成 12 次 HTTP 请求，所以直接结束本票。
        except LLMError as exc:
            errors.append(f"vote={vote_index}: {type(exc).__name__}: {exc}")
            return None
        try:
            decoded = response.json_payload()
            raw_votes.append(
                {
                    "vote_index": vote_index,
                    "validation_attempt": validation_attempt,
                    "payload": decoded,
                    "cached": response.cached,
                }
            )
            return parse_verdict(decoded, sources=sources)
        # HTTP 成功但结构或证据不合格，换一个缓存 key 再要一份回答。
        except (LLMError, ValueError) as exc:
            errors.append(
                f"vote={vote_index}, attempt={validation_attempt + 1}: {type(exc).__name__}: {exc}"
            )
    return None


def _clip(text: str, limit: int) -> str:
    """超长文本保留头尾。测试结论和 diff 收尾都常在末尾。"""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n……（中间省略 {len(text) - limit} 字）……\n{text[-tail:]}"


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


__all__ = [
    "ALLOWED_CATEGORIES",
    "LOW_CONFIDENCE",
    "PROMPT_VERSION",
    "VALIDATION_ATTEMPTS",
    "VOTE_COUNT",
    "AttributionDecision",
    "AttributionInput",
    "AttributionRunError",
    "AttributionVerdict",
    "CompletionClient",
    "EvidenceCitation",
    "FailedTest",
    "attribute_failure",
    "parse_verdict",
    "result_cache_hit",
]
