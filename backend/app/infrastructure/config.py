"""统一配置。

**所有环境变量只在这个文件里读**，别处一律通过 `get_settings()` 拿。
散在各处 `os.environ.get()` 的写法有三个已经吃过亏的问题：默认值对不上、
拼错的变量名要到运行时才发现、以及没人说得清一共有哪些配置项。

配置来源的优先级（pydantic-settings 的默认行为）：

    进程环境变量  >  仓库根目录的 .env  >  这里写的默认值

`.env` 的模板是仓库根目录的 `.env.example`，两边的字段应当保持一致。

## 关于密钥

API Key 一律用 `SecretStr`。它的 `repr()` 是 `SecretStr('**********')`，
所以 `print(settings)`、异常堆栈里的局部变量、FastAPI 的报错页都不会带出明文。
真要用的时候得显式写 `.get_secret_value()` —— 这一下额外动作就是提醒。

只靠 `SecretStr` 不够（拿到明文之后照样能打进日志），第二道防线在
`app/infrastructure/logging.py` 的脱敏处理器里。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.enums import ArtifactBackend

#: 仓库根目录。用它把相对路径配置钉死，不受当前工作目录影响 —— 见 `artifact_local_root`。
#: 层级：config.py → infrastructure → app → backend → 仓库根。
REPO_ROOT = Path(__file__).resolve().parents[3]

#: 数据库默认连接串。
#:
#: 端口用 5433 不用 5432：这台开发机上还有别的项目在用 Postgres，
#: 抢同一个端口会让两边互相起不来，排查起来还特别费时间。
DEFAULT_DATABASE_URL = "postgresql+psycopg://bench:bench@localhost:5433/bench"


def _blank_to_none(value: Any) -> Any:
    """把空字符串当成"没配"。

    `.env.example` 里那些暂时用不上的密钥是留空的（`OPENAI_API_KEY=`）。
    不做这一步的话，`settings.openai_api_key` 会是 `SecretStr('')` 而不是 `None`，
    于是 `if settings.openai_api_key:` 判成真，程序带着一个空 Key 去调 API，
    最后报的是 401 而不是"你没配 Key"。
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


class Settings(BaseSettings):
    """平台的全部配置项。

    字段名是小写下划线，环境变量名是大写（`agent_concurrency` ← `AGENT_CONCURRENCY`），
    pydantic-settings 默认不区分大小写，不用额外声明。

    少数字段的环境变量名和字段名对不上，用 `alias` 显式写出来：

    - `database_url` ← `BENCH_DATABASE_URL`：带前缀是为了和这台机器上别的项目区分开。
    - 各家的 `*_API_KEY` 则**不加前缀**：这些变量要原样传进 Agent 容器，
      被测 CLI（aider、claude-code）自己就认这些名字，改名等于要再做一次映射。
    """

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        # .env 里可能有给别的工具用的变量（比如 COMPOSE_PROJECT_NAME），
        # 不声明就报错的话，每加一个无关变量都要来改这个类
        extra="ignore",
        # 不要开 populate_by_name。开了之后带 alias 的字段会**同时**认字段名，
        # 于是环境里一个裸的 DATABASE_URL 就能盖掉 BENCH_DATABASE_URL——
        # 而 BENCH_ 前缀存在的理由正是"这台机器上还有别的项目也用 Postgres"，
        # 认了裸名字等于把前缀白加了，还可能连到别人的库上。
    )

    # ── 数据库 ──
    database_url: str = Field(default=DEFAULT_DATABASE_URL, alias="BENCH_DATABASE_URL")

    # ── 制品存储（ADR-005：换后端只改这一项，业务代码零改动）──
    artifact_backend: ArtifactBackend = ArtifactBackend.LOCAL
    #: 本地后端的根目录。相对路径按**仓库根目录**解析，不按当前工作目录。
    artifact_local_root: Path = Path("var/artifacts")
    minio_endpoint: str = "localhost:9000"
    minio_access_key: SecretStr | None = None
    minio_secret_key: SecretStr | None = None
    minio_bucket: str = "bench-artifacts"

    # ── 沙箱工作区（E2-T1）──
    #: Git bare mirror 的存放根目录。评测不在运行时 clone GitHub，
    #: 每个仓库在这里留一份镜像，物化工作区时从它 `git archive`（§7.2(1)）。
    #: 和 `artifact_local_root` 一样，相对路径按**仓库根目录**解析。
    mirror_root: Path = Path("var/mirrors")
    #: 每次评测物化出来的代码工作区放这里。目录数随运行次数线性增长，已在 .gitignore 里排除。
    workspace_root: Path = Path("var/workspaces")
    #: 单条 git 命令的超时（秒）。最坏情况是第一次 clone 一个大仓库，还要过代理。
    git_timeout_s: int = Field(default=1800, ge=1)

    # ── 并发（含义见 docs/plan/01-requirements.md §4.6）──
    #: 同时有几个被测 AI 在干活，受服务商限流约束。
    agent_concurrency: int = Field(default=10, ge=1)
    #: 同时跑几个测试容器，受 CPU 和内存约束。
    sandbox_concurrency: int = Field(default=5, ge=1)

    # ── 队列与 Worker（E5-T1，含义见 07-platform-architecture.md §15.2）──
    #: 这个 Worker 进程的标识，写进 `job_queue.lease_owner` 和
    #: `evaluation_task_runs.worker_id`。留空就用 `主机名-进程号`。
    #:
    #: 一台机器上跑多个 Worker 时**建议显式设成固定值**（worker-1、worker-2……）：
    #: 进程号每次重启都变，而启动时回收自己的残留容器要靠这个标识认领。
    worker_id: str | None = None
    #: 租约时长（秒）。必须大于最慢的一道题跑完的时间，否则作业会在还没跑完的时候
    #: 被回收器判成僵尸，交给另一个 Worker 重跑一遍。默认 30 分钟。
    job_lease_s: int = Field(default=1800, ge=1)
    #: 心跳间隔（秒）。心跳线程每隔这么久把租约往后推一次。
    #: 要显著小于 `job_lease_s`，留出网络抖动和 GC 停顿的余量。
    job_heartbeat_s: int = Field(default=60, ge=1)
    #: 队列空的时候隔多久再看一眼（秒）。没有 LISTEN/NOTIFY，就是简单轮询——
    #: 一次评测十几分钟，轮询几秒的延迟可以忽略。
    job_poll_interval_s: float = Field(default=5.0, gt=0)
    #: 一条**作业**最多被领取几次。注意这和评测的重试次数是两回事：
    #: 这个管的是"Worker 崩了 / 处理函数抛异常"，评测的重试次数由协议 C-18
    #: 的映射表决定，见 `app.domain.retry`。
    job_max_attempts: int = Field(default=3, ge=1)
    #: 作业重试的退避基数（秒）。实际等待 `2^attempts × base`，封顶 `job_retry_backoff_cap_s`。
    job_retry_backoff_base_s: float = Field(default=30.0, gt=0)
    job_retry_backoff_cap_s: float = Field(default=3600.0, gt=0)
    #: 收到 SIGTERM 之后，最多再等当前这道题多久（秒）。等不到就放弃等待去做收尾——
    #: 收尾包括回收残留容器，所以宁可等不到也不能跳过。
    worker_shutdown_grace_s: float = Field(default=1200.0, gt=0)
    #: 启动时先回收一遍带 bench 标签的残留容器。
    #: Worker 被 `kill -9` 时容器删不掉，留着会一直占内存和 pid。
    worker_reap_on_start: bool = True
    #: 回收时只删创建超过这么久的容器（秒）。0 表示不管年龄全删。
    #: **一台机器上跑多个 Worker 时必须设成大于最长容器寿命的值**，
    #: 否则新起的 Worker 会把别人正在用的容器删掉。
    worker_reap_min_age_s: float = Field(default=0.0, ge=0)

    # ── 被测 AI 的密钥 ──
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = None
    dashscope_api_key: SecretStr | None = None

    # ── 失败归因用的模型 ──
    judge_model: str | None = None
    judge_api_key: SecretStr | None = None
    judge_base_url: str | None = None

    # ── 其他外部服务 ──
    github_token: SecretStr | None = None
    #: 沙箱出网代理。地址是 WSL 网关，`wsl --shutdown` 之后可能变，不要写死在代码里。
    sandbox_http_proxy: str | None = None
    admin_token: SecretStr | None = None

    # ── 日志 ──
    log_level: str = "INFO"
    #: console 带颜色适合盯着终端看，json 适合被工具消费。开发默认 console，
    #: 部署时在 compose 里设成 json。
    log_format: Literal["json", "console"] = "console"

    # ── 开发期的前端地址（CORS 放行名单，逗号分隔）──
    dev_frontend_origins: str = Field(
        default="http://localhost:3000,http://127.0.0.1:3000",
        alias="BENCH_DEV_FRONTEND_ORIGINS",
    )

    _blank_is_none = field_validator(
        "minio_access_key",
        "minio_secret_key",
        "anthropic_api_key",
        "openai_api_key",
        "deepseek_api_key",
        "dashscope_api_key",
        "judge_model",
        "judge_api_key",
        "judge_base_url",
        "github_token",
        "sandbox_http_proxy",
        "admin_token",
        mode="before",
    )(_blank_to_none)

    @field_validator("artifact_backend", mode="before")
    @classmethod
    def _upper_backend(cls, value: Any) -> Any:
        """`ARTIFACT_BACKEND=local` 和 `=LOCAL` 都认。

        数据库里的枚举值是大写（`ArtifactBackend.LOCAL`），但配置文件里手写小写
        更自然，`.env.example` 里写的也是小写。在这里转一次，省得两边打架。
        """
        return value.upper() if isinstance(value, str) else value

    @field_validator("artifact_local_root", "mirror_root", "workspace_root")
    @classmethod
    def _resolve_root(cls, value: Path) -> Path:
        """相对路径按仓库根目录解析。

        这一步不能省。API 是 `cd backend && uvicorn ...` 起来的，Worker 和 CLI
        可能在仓库根目录起，两边的当前工作目录不一样。要是按当前目录解析，
        `./var/artifacts` 就会变成两个不同的目录，表现是"写进去的制品读不出来"，
        而且不报错。
        """
        return value if value.is_absolute() else (REPO_ROOT / value).resolve()

    @property
    def frontend_origins(self) -> list[str]:
        """CORS 放行名单。空项会被丢掉，避免尾随逗号产生一个空 origin。"""
        return [o.strip() for o in self.dev_frontend_origins.split(",") if o.strip()]

    def secret_values(self) -> list[str]:
        """当前配置里所有非空的密钥明文。

        只给 `configure_logging()` 用：把这些值登记给日志脱敏器，
        这样即使某段被测 AI 的 stdout 里回显了 Key，打日志时也会被替换掉。
        """
        # 按**字段值的类型**筛，不按声明的类型注解筛：注解可能是 `SecretStr | None`，
        # 也可能被 pydantic 归一化成别的等价形式，比对注解容易漏掉字段而不报错。
        values = [
            value.get_secret_value()
            for name in type(self).model_fields
            if isinstance(value := getattr(self, name), SecretStr)
        ]
        return [v for v in values if v]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """取全局配置。

    缓存起来有两个原因：一是每次 `Settings()` 都会重新读一遍 `.env` 文件；
    二是配置在进程生命周期内不应该变 —— 跑到一半换了制品目录，
    前后两批制品会落在不同地方。

    测试里要换配置就调 `reset_settings_cache()`。
    """
    return Settings()


def reset_settings_cache() -> None:
    """清掉配置缓存。只在测试里用。"""
    get_settings.cache_clear()


__all__ = [
    "DEFAULT_DATABASE_URL",
    "REPO_ROOT",
    "Settings",
    "get_settings",
    "reset_settings_cache",
]
