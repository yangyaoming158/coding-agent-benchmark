"""从真实 CLI 的输出里认东西：折行、鉴权失败、报错摘要（E3-T5 抽出来）。

原本这几样都长在 `aider.py` 里。接第二个真实 CLI（Claude Code）时抽出来共用，
理由就写在 `AUTH_MARKERS` 自己的注释里 —— **清单要集中在一处**，散成两份之后，
加一种新的鉴权报错要改两个地方，漏一个就是几百次评测被记成"AI 自己崩了"。

这里只放**和具体 CLI 无关**的部分。认得出 aider 的 `litellm.XxxError`、认得出
Claude Code 的 `result` 事件，那些是各自适配器的事，不往这里塞。

E3-T9（#96）把"这次失败该记在谁头上"的**共同部分**也收到这里：`shared_failure()`。
两个适配器各自判完"这次到底算不算失败"之后都调它，于是"容器被平台杀了"和
"外部服务不让我们用"只有一处判据 —— 散成两份的话，加一条规则要改两个地方，
漏一个就是几十次评测被记成"AI 自己崩了"（这正是 #96 查出来的样子：三个适配器都有）。
"""

from __future__ import annotations

import re

from app.runner.protocol import AUTH_FAILED, EXTERNAL_SERVICE_ERROR, SANDBOX_KILLED, AgentError
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
#: 判错的代价不对称，而且**两个方向的代价不一样**：
#:
#: - 把"外部不让我们用"判成运行时错误 → 按 C-18 算**被测 AI 的错**、不计入平台故障率，
#:   于是解决率安静地掉到 0，而 C-26 的 5% 准入门槛**查不出任何异常**；
#: - 把一次正常运行判成鉴权失败 → 白重试 3 次，而且那道题被记成平台故障。
#:
#: 两个方向 2026-09-12 都真的踩到了（E9-T1），细账见 §18.6。
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
    # 余额不足（2026-09-12 加，E9-T1）。DeepSeek 两个端点的说法一样：
    # aider 那边是 `litellm.BadRequestError: ... "message":"Insufficient Balance"`，
    # Claude Code 那边是 `API Error: 402 Insufficient Balance`。
    #
    # **为什么归到"鉴权"这一类**：余额不足严格讲是计费问题不是凭据问题，但这个清单
    # 决定的是**这笔账归谁**，而 `AGENT_AUTH_ERROR` 的归属是 `EXTERNAL`、计入平台
    # 故障率（C-18）—— 这正是余额不足该有的账：外部服务不让我们用，不是被测 AI 改错了。
    # 单开一个 `AGENT_BILLING_ERROR` 更准确，但那要动协议的 `infra_outcome` 枚举
    # （冻结件），得走 §9 的变更流程，另提提案。
    "insufficientbalance",
)

#: HTTP 401（没授权）和 402（要付钱）。单独用正则而不是塞进上面的清单：
#: 裸写 `"401"` 会被 `Tokens: 1401 sent` 命中，于是一次正常的运行被判成鉴权失败。
#:
#: **前后都要挡住数字和小数点**，光用 `\b` 不够 —— 2026-09-12 实测被
#: aider 的进度条命中过：`142/142 [00:00<00:00, 401.79it/s]` 里的 `401`
#: 两侧分别是 `,` 和 `.`，`\b401\b` 照样匹配，于是一次**本来只是余额不足**的运行
#: 被判成鉴权失败。`\b` 把小数点当词边界，而吞吐量天生带小数。
AUTH_STATUS_RE = re.compile(r"(?<![\d.])40[12](?![\d.])")


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


#: 外部服务的另外几种"不让我们用"（E3-T9）：限流、供应商 5xx、连不上。和 `AUTH_MARKERS`
#: 记同一笔账（`AGENT_AUTH_ERROR`，归属 EXTERNAL、计入平台故障率），码分开成
#: `external_service_error`，事后翻记录分得清"Key 配错了"和"对面挂了"。
#:
#: **每一条都带锚，不认裸词。** 被测 AI 的对话记录里什么词都有：本机 1931 份真实 agent 日志里
#: 52 份含 "429"、3 份含 "overloaded"，全是 AI 在讨论代码，没有一次是真的限流。而且 HTTP 的
#: 标准短语挤掉空白之后就是异常类名 —— `500 Internal Server Error` → `internalservererror`，
#: 一条失败的测试输出就能把 AI 自己的失败判给外部服务、白重试 3 次、还多记一次平台故障。
#: 所以只认三种带前缀的形态，都是 CLI 自己打出来的、AI 的对话里不会原样出现：
#:
#: 1. aider：`litellm.RateLimitError: ...`（带 `litellm.` 前缀的异常类名）；
#: 2. Claude Code：stream-json 里 `"text":"API Error: 429 ..."` / `"result":"API Error: 529 ..."`
#:    （错误文本就是整个字段的开头 —— 2026-09-12 那 44 次 402 的真实样子，两个字段各 44 / 88 次）；
#: 3. openai SDK 风格的 `Error code: 503 - {...}`（有的中转端点这么报）。
#:
#: 4xx 里只收 429：400 是我们请求写错了、404 是模型名配错了，那些是配置问题，不是对面挂了。
LITELLM_EXTERNAL_ERRORS: tuple[str, ...] = (
    "RateLimitError",
    "ServiceUnavailableError",
    "InternalServerError",
    # 连不上供应商。在沙箱里这可能是我们的网络也可能是对面，反正不是被测 AI 改错了代码
    "APIConnectionError",
)

#: 在挤掉空白、转小写之后的文本上匹配（理由见 `squash()`）。
EXTERNAL_SERVICE_RE = re.compile(
    r"litellm\.(?:" + "|".join(name.lower() for name in LITELLM_EXTERNAL_ERRORS) + r")"
    r'|"(?:text|result)":"apierror:?(?:429|5\d\d)(?!\d)'
    r"|errorcode:(?:429|5\d\d)-\{"
)


def looks_like_external_service_failure(text: str) -> bool:
    """这段输出像不像"外部服务不让我们用"（限流 / 供应商 5xx / 连不上）。

    鉴权和余额不足另有 `looks_like_auth_failure()`，两者的账落在同一个
    `AGENT_AUTH_ERROR` 上，只是错误码不同。
    """
    return bool(EXTERNAL_SERVICE_RE.search(squash(text)))


def killed_silently(container: ContainerResult) -> bool:
    """容器一个字节都没输出就被 SIGKILL 了，而且没人认领（不是 OOM、不是我们超时杀的）。

    这是 §18.7 记的孤儿回收误杀的样子：第二个 Worker 启动时把老 Worker 正在用的
    8 个容器当孤儿收了，表现是 `exit_code=137 / stdout_bytes=0 / stderr_bytes=0`、6 秒就死。
    那 8 次当时被记成 `AGENT_RUNTIME_ERROR`，也就是算到被测 AI 头上。

    **"没输出"这个条件不能去掉。** 有输出的 137 还有另一种解释 —— dockerd 漏收了 OOM
    通知（并发时约 5%，`05-sandbox.md` §10.10），那种按协议 C-06/C-07 不能用退出码判成
    OOM，要改得走 C-51。所以这里只认"还没来得及说话就死了"这一种，别的 137 维持原判。
    """
    return (
        container.sigkilled_without_oom_flag
        and not container.stdout.strip()
        and not container.stderr.strip()
    )


def shared_failure(container: ContainerResult) -> AgentError | None:
    """一次**已经判定为失败**的运行，先看是不是平台或外部服务的锅（E3-T9，AC 3：判据集中一处）。

    调用方（各适配器的 `_error_for()`）先自己判 OOM、超时和"这次算不算失败"——
    那些和具体 CLI 有关；然后把剩下的交给这里。返回 None 表示这里认不出来，
    调用方按自己的措辞报 `runtime_error`，责任落在被测 AI 这一侧。

    顺序：先看容器是不是被杀的（没输出的话下面的文本判据本来也没东西可看），
    再看鉴权 / 余额，最后看限流 / 5xx。
    """
    if killed_silently(container):
        return AgentError(
            code=SANDBOX_KILLED,
            message=(
                "Agent 容器没输出一个字节就被 SIGKILL（退出码 137，docker 没标 OOM，"
                "也不是我们超时杀的）—— 像是被平台自己回收了，见 §18.7"
            ),
        )
    text = container.stdout + "\n" + container.stderr
    excerpt = failure_excerpt(container)
    if looks_like_auth_failure(text):
        return AgentError(code=AUTH_FAILED, message=f"疑似鉴权失败：{excerpt}")
    if looks_like_external_service_failure(text):
        return AgentError(
            code=EXTERNAL_SERVICE_ERROR, message=f"外部服务失败（限流 / 5xx / 连不上）：{excerpt}"
        )
    return None


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
    "EXTERNAL_SERVICE_RE",
    "LITELLM_EXTERNAL_ERRORS",
    "failure_excerpt",
    "killed_silently",
    "looks_like_auth_failure",
    "looks_like_external_service_failure",
    "shared_failure",
    "squash",
    "unwrap",
]
