"""结构化日志。

一次评测要跨编排器、沙箱、判定引擎好几层，出问题时最先要回答的问题是
"这行日志是哪次实验、哪道题打出来的"。所以日志不写成一句话，
写成一组字段：

    {"event": "容器已退出", "run_id": 12, "task_run_id": 340,
     "exit_code": 137, "oom_killed": true, "level": "info", ...}

这样才能直接按 `task_run_id` 过滤出一道题的完整轨迹。

## 上下文怎么带

`run_id` / `task_run_id` 不靠层层传参，用 `bind_run_context()` 绑一次，
之后这个上下文里的每条日志都自动带上：

    with bind_run_context(run_id=12, task_run_id=340):
        log.info("开始物化工作区")     # 自动带 run_id 和 task_run_id

底层是 `contextvars`。**注意**：新起的线程拿不到父线程绑好的上下文
（`threading.Thread` 从空上下文开始）。Worker 用线程池并发时，
要在每个任务函数**内部**重新 `bind_run_context()`，不能只在派发前绑一次。

## 脱敏

这个项目要接好几家大模型服务商，密钥泄漏风险高，而日志是最容易漏的地方 ——
尤其是被测 AI 的 stdout，CLI 工具启动时打一行自己的配置就把 Key 带出来了。

三道防线，都在 `redact_secrets` 这个处理器里：

1. **按字段名**：`api_key`、`github_token` 这类字段，值整个换掉。
2. **按已登记的明文**：`configure_logging()` 会把当前配置里的密钥登记进来，
   任何字符串里出现这些值都会被替换 —— 这条管的就是"AI 自己把 Key 打出来了"。
3. **按已知格式**：`sk-...`、`ghp_...` 这些形状，即使没在配置里出现过也照样挡。

`scripts/check_secrets.py` 里有一份相似的正则表，两边是**故意分开**的：
那个是提交前扫源码的 git 钩子，要求零依赖能单独跑；这个是运行期洗日志的，
两者的输入和时机都不一样，合并只会让两边都变别扭。
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import IO, Any, cast

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from app.infrastructure.config import Settings, get_settings

#: 被打掉的值统一换成这个。写得显眼一点，方便一眼看出"这里原本有东西"。
MASK = "***REDACTED***"

#: 字段名以这些词**结尾**就认为是敏感字段。
#:
#: 用"结尾"而不是"包含"是为了避开误伤：这个项目要记 token 用量，
#: `prompt_tokens`、`total_tokens` 是正经数据，用"包含 token"会把它们一起打掉，
#: 费用统计就没法查了。`total_tokens` 结尾是 tokens 不是 token，所以不会命中。
_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key"
    r"|password|passwd|credentials?|authorization|token|secret)$",
    re.IGNORECASE,
)

#: 各家密钥的形状。sk-ant- 放在 sk- 前面，让 Anthropic 的 Key 命中更具体的那条。
_SECRET_VALUE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bLTAI[A-Za-z0-9]{12,}"),
]

#: 运行期登记的密钥明文（来自 Settings）。
_registered_secrets: set[str] = set()

#: 登记密钥的最短长度。太短的值（比如有人把 ADMIN_TOKEN 设成 "dev"）
#: 会把日志里所有含 "dev" 的字符串都打掉，那样日志就没法看了。
_MIN_SECRET_LEN = 8

#: 嵌套结构最多往下洗几层。日志字段偶尔会带一个小 dict，但不该带一棵深树；
#: 设个上限是防着有人把整个响应体塞进来，让日志处理器空转。
_MAX_DEPTH = 4


def register_secret(value: str) -> None:
    """登记一个需要在日志里被替换掉的明文。太短的忽略。"""
    if len(value) >= _MIN_SECRET_LEN:
        _registered_secrets.add(value)


def clear_registered_secrets() -> None:
    """清空已登记的密钥。只在测试里用。"""
    _registered_secrets.clear()


def _mask_text(text: str) -> str:
    """洗一个字符串：先替换已登记的明文，再按已知格式兜底。"""
    for secret in _registered_secrets:
        if secret in text:
            text = text.replace(secret, MASK)
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub(MASK, text)
    return text


def _scrub(value: Any, depth: int = 0) -> Any:
    """递归洗一个值。dict 的键名敏感就整值替换，字符串按内容替换。"""
    if depth > _MAX_DEPTH:
        return value
    if isinstance(value, str):
        return _mask_text(value)
    if isinstance(value, Mapping):
        return {
            key: MASK if _SENSITIVE_KEY_RE.search(str(key)) else _scrub(item, depth + 1)
            for key, item in value.items()
        }
    # str 也是 Sequence，前面已经拦掉了；bytes 不洗（日志里不该出现原始字节）
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_scrub(item, depth + 1) for item in value]
    return value


def redact_secrets(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """structlog 处理器：把事件里的密钥擦掉。

    放在渲染器**之前**、其他处理器之后：那时候字段已经齐了（包括异常堆栈
    被 `format_exc_info` 转成的文本），但还没变成一行字符串，仍然能按字段处理。
    """
    return cast(EventDict, _scrub(dict(event_dict)))


class RedactingFormatter(logging.Formatter):
    """在最后一道口子上再洗一遍。

    structlog 的处理器只管我们自己打的日志，管不到 uvicorn、SQLAlchemy 这些
    直接用 stdlib logging 的库 —— 而它们照样会漏：SQLAlchemy 打连接串时带着密码，
    uvicorn 记访问日志时带着 URL 上的 token。

    handler 是所有日志的必经之路，在这里洗一遍，覆盖面比在 structlog 链里洗大得多。
    这一层只按内容洗（已登记的明文 + 已知格式），按字段名洗是 `redact_secrets` 的活 ——
    到了这里字段已经拍平成一行字符串，看不出哪个是字段名了。
    """

    def format(self, record: logging.LogRecord) -> str:
        return _mask_text(super().format(record))


class _StdlibBoundLogger(structlog.stdlib.BoundLogger):
    """把 `.exception()` 接到 `error()` 上。

    不改的话堆栈会被打两遍：structlog 的 `format_exc_info` 已经把堆栈放进
    `exception` 字段了，而 `logging.Logger.exception()` 内部会强制再设一次
    `exc_info=True`，于是 handler 又在 JSON 后面追加一份**没脱敏**的原文。
    两份内容一样，但只有 JSON 里那份洗过。
    """

    def exception(self, event: str | None = None, *args: Any, **kw: Any) -> Any:
        kw.setdefault("exc_info", True)
        return self._proxy_to_logger("error", event, *args, **kw)


def _build_processors(log_format: str, *, colors: bool) -> list[Processor]:
    """拼处理器链。顺序有讲究，见每一项的注释。"""
    processors: list[Processor] = [
        # 必须第一个：把 bind_run_context() 绑的 run_id/task_run_id 并进事件
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        # 把异常堆栈转成 `exception` 字段，这样一条 JSON 就是完整的一条记录，
        # 不会出现"JSON 一行、堆栈跟在后面好几行"导致 jq 解析不了。
        # 放在脱敏之前：堆栈里可能带着密钥（401 的响应体常被原样塞进异常消息），
        # 先变成文本才洗得到。
        structlog.processors.format_exc_info,
        redact_secrets,
    ]
    if log_format == "json":
        # ensure_ascii=False：中文日志直接可读，不然全是 \uXXXX
        processors.append(structlog.processors.JSONRenderer(ensure_ascii=False))
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=colors))
    return processors


def configure_logging(settings: Settings | None = None, *, stream: IO[str] | None = None) -> None:
    """配好日志。进程启动时调用一次（API 在 `create_app()` 里，Worker 在入口处）。

    `stream` 只给测试用：把输出接到 StringIO 上，就能断言真实渲染出来的内容，
    而不是只测处理器函数本身。
    """
    settings = settings or get_settings()

    # 把配置里的密钥登记进脱敏器。这一步是"被测 AI 回显了 Key"那条防线的来源。
    for value in settings.secret_values():
        register_secret(value)

    target = stream if stream is not None else sys.stdout
    level = logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)

    # 整个替换 root 的 handler，不用 addHandler：这个函数可能被调用多次
    # （测试、uvicorn --reload），addHandler 会让同一条日志打印好几遍。
    handler = logging.StreamHandler(target)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    colors = bool(getattr(target, "isatty", lambda: False)())
    structlog.configure(
        processors=_build_processors(settings.log_format, colors=colors),
        wrapper_class=_StdlibBoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """取一个日志器。名字一般传 `__name__`。"""
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))


@contextmanager
def bind_run_context(
    *,
    run_id: int | None = None,
    task_run_id: int | None = None,
    task_id: str | None = None,
    agent_id: int | None = None,
    attempt: int | None = None,
) -> Iterator[None]:
    """在这个代码块里打的日志自动带上这些字段。

    传 None 的字段会被跳过，不会打出 `"task_run_id": null` 这种噪声。
    退出代码块时恢复到进来之前的值，所以可以嵌套：
    外层绑 `run_id`，内层每道题再绑 `task_run_id`。
    """
    fields = {
        "run_id": run_id,
        "task_run_id": task_run_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "attempt": attempt,
    }
    with structlog.contextvars.bound_contextvars(
        **{key: value for key, value in fields.items() if value is not None}
    ):
        yield


__all__ = [
    "MASK",
    "RedactingFormatter",
    "bind_run_context",
    "clear_registered_secrets",
    "configure_logging",
    "get_logger",
    "redact_secrets",
    "register_secret",
]
