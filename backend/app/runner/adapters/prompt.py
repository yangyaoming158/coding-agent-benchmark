"""下发给真实 CLI 的那段提示词（E3-T5 抽出来）。

**所有真实适配器必须共用这一份。** 这不是为了少写几行代码 —— 排行榜比的是
"哪个 Agent 更会修 bug"，如果 aider 拿到的题面和 Claude Code 的不一样，
比出来的就是"哪段提示词写得好"。同一道题、同一段话，剩下的差异才归 Agent。

哨兵适配器（Oracle / Noop / Mock）用不到这里：它们不调模型，直接交补丁。
"""

from __future__ import annotations

from app.domain.enums import IssueLanguage
from app.runner.protocol import AgentTaskInput

#: 提示词的骨架。issue 用什么语言就用哪一份 —— 中文题干配英文指令，
#: 模型有时会跟着指令切回英文回答，那会让轨迹和日志变得难读。
PROMPT_TEMPLATES: dict[IssueLanguage, str] = {
    IssueLanguage.ZH: (
        "请修复下面这个缺陷。\n\n"
        "## {title}\n\n{body}\n\n"
        "---\n\n"
        "几点要求：\n\n"
        "1. 只改产品代码。测试文件（{protected}）就算改了也会被丢弃，别在上面花时间。\n"
        "2. 不要新增第三方依赖，跑测试的容器是断网的，装不上。\n"
        "3. 改完就结束，不用写解释，也不用写总结。\n"
    ),
    IssueLanguage.EN: (
        "Please fix the bug described below.\n\n"
        "## {title}\n\n{body}\n\n"
        "---\n\n"
        "Requirements:\n\n"
        "1. Only change production code. Edits to test files ({protected}) are discarded, "
        "so do not spend effort there.\n"
        "2. Do not add third-party dependencies; the test container has no network.\n"
        "3. Stop when the fix is in place. No explanation or summary is needed.\n"
    ),
}


def build_task_prompt(task: AgentTaskInput) -> str:
    """拼给 CLI 的那段话。

    `protected_paths` 直接来自 `task.constraints`，那是
    `agent_visible_patterns()` 的产物（通用规则），**不含**该题的
    `test_patch_paths` —— 后者下发出去等于告诉 AI 官方改了哪几个文件（协议 C-76）。
    这里原样用，不要自己另拼一份。
    """
    template = PROMPT_TEMPLATES.get(task.issue.language, PROMPT_TEMPLATES[IssueLanguage.EN])
    return template.format(
        title=task.issue.title,
        body=task.issue.body,
        protected=", ".join(task.constraints.protected_paths) or "tests/**",
    )


__all__ = ["PROMPT_TEMPLATES", "build_task_prompt"]
