"""从真实 CLI 的输出里认东西：折行、鉴权失败、报错摘要（E3-T5 抽出来）。

原本这几样都长在 `aider.py` 里。接第二个真实 CLI（Claude Code）时抽出来共用，
理由就写在 `AUTH_MARKERS` 自己的注释里 —— **清单要集中在一处**，散成两份之后，
加一种新的鉴权报错要改两个地方，漏一个就是几百次评测被记成"AI 自己崩了"。

这里只放**和具体 CLI 无关**的部分。认得出 aider 的 `litellm.XxxError`、认得出
Claude Code 的 `result` 事件，那些是各自适配器的事，不往这里塞。
"""

from __future__ import annotations

import re

from app.sandbox.container import ContainerResult

#: `AgentError.message` 里最多放多少字符（两条流平分）。太短看不出问题，
#: 太长会把 `evaluation_task_runs.error_message_excerpt` 撑爆。
ERROR_EXCERPT_CHARS = 2000

#: 判成鉴权失败的标记。**写成挤掉空白之后的样子**，理由见 `squash()`。
#:
#: 这里只能靠子串：对面是外部 CLI 打出来的自由文本，没有一张可查的表。
#: 所以把清单集中放在这一处，不要散到各个适配器里 —— 散开之后，加一种新的鉴权报错
#: 要改几个地方，漏一个就是几百次评测被记成"AI 自己崩了"。
#:
#: 判错的代价不对称：鉴权失败按 C-18 重试 3 次、运行时错误重试 1 次。
#: 把鉴权当成运行时错误，一个配错的 Key 会安静地把解决率拉到 0。
AUTH_MARKERS: tuple[str, ...] = (
    "authenticationerror",
    "authentication_error",
    "authenticationfails",
    "invalid_api_key",
    "incorrectapikey",
    "invalidapikey",
    "noapikey",
    "apikeynotfound",
    "unauthorized",
    # 下面两条是 Claude Code 的说法（2026-09-06 加）。它不打 litellm 的异常名，
    # 报的是自己那套文案，光靠上面几条认不出来
    "invalidbearertoken",
    "oauthtokenexpired",
)

#: HTTP 401。单独用正则而不是塞进上面的清单：裸写 `"401"` 会被
#: `Tokens: 1401 sent` 命中，于是一次正常的运行被判成鉴权失败。
AUTH_STATUS_RE = re.compile(r"\b401\b")


def unwrap(text: str) -> str:
    """把折行接回去：连续空白（含换行）压成**一个空格**。

    CLI 按终端宽度折行，一条用量行经常被劈成两半。2026-09-05 在 aider 上
    抓到的三种劈法：

        Cost: $0.00050 message, $0.00050\nsession.
        Cost: $0.0012 message, $0.0012 \nsession.
        Cost: $0.00074 message, \n$0.00074 session.

    劈在哪儿看消息本身有多长，没有规律。压成一个空格之后这三种都一样了。

    和 `squash()` 的区别：那个压成**空**，用来做子串匹配，顺带把被劈开的单词接回去；
    这个压成**一个空格**，用来做正则匹配，词与词的边界必须留着。
    """
    return re.sub(r"\s+", " ", text)


def squash(text: str) -> str:
    """挤掉全部空白并转小写，再拿去比对。

    CLI 按终端宽度硬折行，而且**会从单词中间折**。2026-09-05 在 aider 上抓到的原文：

        litellm.BadRequestError: DeepseekException - {"error":{"message":"Authentication
        Fails, Your api key: ****9ca8 is
        invalid","type":"authentication_error","param":null,"code":"invalid_request_erro
        r"}}

    `Authentication Fails` 被折成了两行，`invalid_request_error` 被从
    `erro | r` 中间劈开。照原样做子串匹配的话，一段报错认不认得出来取决于它
    恰好折在哪个字符上 —— 这种 bug 只在某些消息长度下出现，最难复现。

    挤掉空白之后这两个问题一起没了，代价是清单里的标记也要写成没有空格的形式。
    """
    return re.sub(r"\s+", "", text).lower()


def looks_like_auth_failure(text: str) -> bool:
    """这段输出像不像鉴权失败。清单见 `AUTH_MARKERS`。"""
    squashed = squash(text)
    return any(marker in squashed for marker in AUTH_MARKERS) or bool(
        AUTH_STATUS_RE.search(squashed)
    )


def failure_excerpt(container: ContainerResult) -> str:
    """给人看的报错摘要，两条流各截一段尾巴。

    **不能只取 stderr。** 这两个 CLI 都把模型侧的报错打在 **stdout** 上，stderr 里
    往往只有一句 `Warning: Input is not a terminal (fd=0).` —— 只取 stderr 的话，
    `evaluation_task_runs.error_message_excerpt` 那一列里就只剩这句废话，
    而真正的原因在 stdout 里躺着（2026-09-05 实测踩到）。

    两条各截一半而不是拼起来再截：拼完再截的话，stderr 一长，stdout 的尾巴
    （报错就在那儿）会被挤掉。
    """
    half = ERROR_EXCERPT_CHARS // 2
    parts = [
        f"{label}: {stream.strip()[-half:]}"
        for label, stream in (("stdout", container.stdout), ("stderr", container.stderr))
        if stream.strip()
    ]
    return "\n".join(parts)


__all__ = [
    "AUTH_MARKERS",
    "AUTH_STATUS_RE",
    "ERROR_EXCERPT_CHARS",
    "failure_excerpt",
    "looks_like_auth_failure",
    "squash",
    "unwrap",
]
