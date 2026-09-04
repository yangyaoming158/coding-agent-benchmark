"""Settings 的测试（E0-T4）。

重点在两件事上：**密钥不能明文露出来**，以及**相对路径的解析基准是仓库根目录
而不是当前工作目录**。后者是个不报错的坑：API 从 `backend/` 起、Worker 从仓库根起，
按当前目录解析的话两边会写到不同的制品目录里，表现是"存进去的日志读不出来"。

所有用例都传 `_env_file=None`，不读开发机上真实的 `.env` ——
不然谁的机器上配了哪些 Key，测试结果就跟着变。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.enums import ArtifactBackend
from app.infrastructure.config import (
    DEFAULT_DATABASE_URL,
    REPO_ROOT,
    Settings,
    get_settings,
    reset_settings_cache,
)


def _fake_secret(prefix: str, body: str) -> str:
    """拼一个假密钥出来。

    **必须拼，不能整串写在源码里**，否则 `scripts/check_secrets.py` 扫到这个
    测试文件自己就会报警，提交被钩子拦下、CI 变红 —— 这个坑
    `test_repo_guards.py` 里已经踩过一次了。

    注意 `make check` **不跑**密钥扫描（它是 pre-commit 钩子 + 独立的 CI job），
    所以本地全绿也发现不了这个问题。
    """
    return prefix + body


#: 一个长得像真 Key 的假值。故意用 Anthropic 的前缀，顺便覆盖日志脱敏那条正则。
FAKE_KEY = _fake_secret("sk-ant-", "api03-FAKEFAKEFAKEFAKEFAKEFAKEFAKE")

#: 别名和字段名对不上的那几个。
ALIASES = ("BENCH_DATABASE_URL", "BENCH_DEV_FRONTEND_ORIGINS")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """把开发机上真实的配置从环境里摘掉，让每个用例从干净状态开始。"""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    for alias in ALIASES:
        monkeypatch.delenv(alias, raising=False)
    reset_settings_cache()


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


# ── 默认值 ─────────────────────────────────────────────────


def test_defaults() -> None:
    settings = make_settings()
    assert settings.database_url == DEFAULT_DATABASE_URL
    assert settings.artifact_backend is ArtifactBackend.LOCAL
    assert settings.agent_concurrency == 10
    assert settings.sandbox_concurrency == 5
    assert settings.log_format == "console"


def test_database_url_uses_prefixed_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """连接串的环境变量名是 `BENCH_DATABASE_URL`，不是 `DATABASE_URL`。

    加前缀是为了和这台机器上别的项目区分开（它们也用 Postgres，端口都撞过一次）。
    """
    monkeypatch.setenv("BENCH_DATABASE_URL", "postgresql+psycopg://u:p@localhost:5555/other")
    assert make_settings().database_url.endswith("/other")

    # 裸的 DATABASE_URL 必须被无视：环境里有这个变量太常见了，
    # 认它等于把前缀白加了，还可能把评测数据写进别的项目的库
    monkeypatch.delenv("BENCH_DATABASE_URL")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:6666/wrong")
    assert make_settings().database_url == DEFAULT_DATABASE_URL


# ── 密钥 ───────────────────────────────────────────────────


def test_blank_secret_becomes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """`.env.example` 里留空的 Key 要变成 None，不是空的 SecretStr。

    不做这一步的话 `if settings.openai_api_key:` 判成真，程序带着空 Key 去调 API，
    报出来的是 401，查半天才发现其实是"根本没配"。
    """
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("GITHUB_TOKEN", "   ")
    settings = make_settings()
    assert settings.openai_api_key is None
    assert settings.github_token is None


def test_secret_not_exposed_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """把整个 Settings 打出来，不能带出明文。

    这是第一道防线：异常堆栈里的局部变量、FastAPI 的报错页、随手一句 print，
    都会走 repr。第二道防线在日志脱敏器里。
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    settings = make_settings()
    assert FAKE_KEY not in repr(settings)
    assert FAKE_KEY not in str(settings)
    # 真要用的时候得显式取，这一下额外动作就是提醒
    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == FAKE_KEY


def test_secret_values_collects_only_nonempty(monkeypatch: pytest.MonkeyPatch) -> None:
    """`secret_values()` 交给日志脱敏器的，只能是真配了的那些。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert make_settings().secret_values() == [FAKE_KEY]


def test_secret_values_empty_when_nothing_configured() -> None:
    assert make_settings().secret_values() == []


# ── 制品存储 ───────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["local", "LOCAL", "Local"])
def test_artifact_backend_accepts_any_case(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """数据库枚举是大写，`.env` 里手写小写更自然，两种都得认。"""
    monkeypatch.setenv("ARTIFACT_BACKEND", raw)
    assert make_settings().artifact_backend is ArtifactBackend.LOCAL


def test_relative_artifact_root_is_anchored_to_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """相对路径按**仓库根目录**解析，换个工作目录结果不变。

    这条不成立的话，`cd backend && uvicorn` 起的 API 会写到 `backend/var/artifacts`，
    而在仓库根目录起的 Worker 写到 `var/artifacts`，两边互相看不见对方的制品。
    """
    monkeypatch.setenv("ARTIFACT_LOCAL_ROOT", "var/artifacts")
    from_repo_root = make_settings().artifact_local_root

    monkeypatch.chdir(tmp_path)
    from_elsewhere = make_settings().artifact_local_root

    assert from_repo_root == from_elsewhere == (REPO_ROOT / "var/artifacts").resolve()


def test_absolute_artifact_root_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """绝对路径原样保留 —— 部署时就是写绝对路径（§17.1 的 /var/lib/bench/artifacts）。"""
    monkeypatch.setenv("ARTIFACT_LOCAL_ROOT", "/var/lib/bench/artifacts")
    assert make_settings().artifact_local_root == Path("/var/lib/bench/artifacts")


@pytest.mark.parametrize(
    ("env_name", "field", "default_suffix"),
    [
        ("MIRROR_ROOT", "mirror_root", "var/mirrors"),
        ("WORKSPACE_ROOT", "workspace_root", "var/workspaces"),
    ],
)
def test_sandbox_roots_are_anchored_to_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env_name: str, field: str, default_suffix: str
) -> None:
    """镜像根和工作区根也按仓库根目录解析，理由和制品目录一样。

    这里更要命一点：Worker 从 `var/mirrors` 里 `git archive`，CLI 在别处把镜像
    拉到另一个 `var/mirrors`，表现是"明明拉过了还说找不到 commit"。
    """
    monkeypatch.delenv(env_name, raising=False)
    from_repo_root = getattr(make_settings(), field)

    monkeypatch.chdir(tmp_path)
    from_elsewhere = getattr(make_settings(), field)

    assert from_repo_root == from_elsewhere == (REPO_ROOT / default_suffix).resolve()


# ── 其他字段 ───────────────────────────────────────────────


def test_frontend_origins_splitting(monkeypatch: pytest.MonkeyPatch) -> None:
    """逗号分隔，顺手丢掉空白和尾随逗号 —— 空 origin 会让 CORS 中间件行为古怪。"""
    monkeypatch.setenv("BENCH_DEV_FRONTEND_ORIGINS", " http://a:3000 , http://b:3000 ,")
    assert make_settings().frontend_origins == ["http://a:3000", "http://b:3000"]


@pytest.mark.parametrize("value", ["0", "-1"])
def test_concurrency_must_be_at_least_one(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """并发数配成 0 就是"一个任务都不跑"，让它在启动时报错，别静默挂住。"""
    monkeypatch.setenv("AGENT_CONCURRENCY", value)
    with pytest.raises(ValidationError):
        make_settings()


def test_env_file_is_read_and_extra_keys_ignored(tmp_path: Path) -> None:
    """.env 能读进来；里面有不认识的变量也不该报错。

    `.env` 是人手写的，可能混着给别的工具用的变量（COMPOSE_PROJECT_NAME 之类）。
    每加一个无关变量就要回来改 Settings 类，这个规则撑不过一周。
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AGENT_CONCURRENCY=3\nSANDBOX_CONCURRENCY=2\nCOMPOSE_PROJECT_NAME=bench\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert (settings.agent_concurrency, settings.sandbox_concurrency) == (3, 2)


# ── 缓存 ───────────────────────────────────────────────────


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一个进程里配置只读一次。

    缓存不只是省 IO：跑到一半换了制品目录，前后两批制品会落在不同地方。
    """
    monkeypatch.setenv("AGENT_CONCURRENCY", "7")
    first = get_settings()
    assert first.agent_concurrency == 7
    assert get_settings() is first

    monkeypatch.setenv("AGENT_CONCURRENCY", "9")
    assert get_settings().agent_concurrency == 7, "没清缓存就不该跟着变"

    reset_settings_cache()
    assert get_settings().agent_concurrency == 9
