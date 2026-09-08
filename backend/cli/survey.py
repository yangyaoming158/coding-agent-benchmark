"""仓库选型实测（E8-T1，`03-benchmark-spec.md` §8.3）。

    python -m cli.survey probe              # 第一段：只查 GitHub，几分钟
    python -m cli.survey measure            # 第二段：进容器实测安装和测试耗时
    python -m cli.survey report             # 打分表

`make survey` 是「probe + report」的快捷方式。

## 为什么分两段

`pip install` 是整件事里最贵的一步，一个仓库几分钟起步。先花几十个 API 调用把
明显不合格的刷掉（归档了、许可证是 GPL、候选池不够深），剩下的才值得等。

两段共用一份 `datasets/survey/repos-<日期>.json`：`probe` 建它，`measure` 往同一条
记录上补字段。所以第二段可以隔一天再跑，也可以只跑其中几个仓库。

## 第二段测的是"评测时的耗时"，不是"最快能多快"

容器限额按 `benchmark_tasks` 的默认值给（2 CPU / 2048 MB），和真实评测一致。
拿 16 核跑出来的数字去对 §8.3 的 120 秒 / 180 秒门槛没有意义 —— 真评测的时候
一台机器上要同时跑好几个容器。

## 这条命令会联网，而且会装包

`probe` 打 GitHub API，`measure` 会 clone 仓库并在容器里 `pip install`。
容器走桥接网络（不是评测阶段的 `--network none`）—— 装依赖本来就得联网，
而这里跑的是公开仓库的测试，不涉及防作弊。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.benchmark.github import (
    GitHubError,
    GitHubNotFoundError,
    gh_available,
    graphql,
    rest,
)
from app.benchmark.survey import (
    CANDIDATE_WINDOW_DAYS,
    MAX_INSTALL_S,
    MAX_TEST_S,
    MIN_CANDIDATE_PRS,
    MIN_PYTHON_RATIO,
    RepoFacts,
    count_issue_languages,
    count_pr_candidates,
    evaluate,
    python_ratio,
    rank,
    shortlist,
)
from app.infrastructure.config import REPO_ROOT
from app.sandbox.container import (
    DETERMINISM_ENV,
    WORKSPACE_TARGET,
    BindMount,
    ContainerResult,
    ContainerSpec,
    NetworkMode,
    ResourceLimits,
    SandboxError,
    Stage,
    run_in_container,
)
from app.sandbox.git_cli import GitError, run_git

SURVEY_DIR = REPO_ROOT / "datasets" / "survey"
CANDIDATES_FILE = SURVEY_DIR / "candidates.txt"

#: 实测用的基础镜像。选 slim 而不是自己建一个：这一步测的是"这个仓库好不好装"，
#: 镜像里预置的东西越少，量出来的数字越接近 E2-T3 从头建 env 镜像时的真实开销。
MEASURE_IMAGE = "python:3.11-slim"

#: **跑测试**时的容器限额，和 `benchmark_tasks` 的默认值一致（见迁移 0001）。
#: 测试耗时要能和正式评测对得上，所以这里必须用评测时的限额。
TEST_LIMITS = ResourceLimits(cpus=2.0, memory_mb=2048, pids_limit=512)

#: **装依赖**时的容器限额，比跑测试宽。
#:
#: 装依赖在生产里发生在**建镜像**的时候（ADR-008、E2-T3），不在评测沙箱里，
#: 所以拿评测的限额去卡它是错的。torch 那一套光解包就要好几个 GB。
BUILD_LIMITS = ResourceLimits(cpus=4.0, memory_mb=6144, pids_limit=1024)

#: pip 的临时目录。**必须落到挂载进来的磁盘上，不能用容器的 `/tmp`** ——
#: `/tmp` 是 tmpfs，吃的是容器内存额度，装 torch 那一套会直接
#: `OSError: [Errno 28] No space left on device`（2026-09-08 实测，
#: xorbitsai/inference 和 LlamaFactory 都栽在这儿，看起来像"这个仓库装不上"）。
#:
#: 这条对 E2-T3 同样成立：建 env 镜像时也要给足临时空间。
PIP_TMPDIR = f"{WORKSPACE_TARGET}/.bench-pip-tmp"

#: 容器里的 HOME。**也必须落到磁盘上。**
#:
#: 容器跟着宿主机 uid 跑，写不进系统 site-packages，pip 于是退回 user 安装、
#: 装到 `$HOME/.local`。HOME 指向 `/tmp`（tmpfs）的话，装的包全进内存，
#: 大项目照样 `No space left on device` —— 只是错误信息换了个位置
#: （2026-09-08：TMPDIR 挪到磁盘之后 LlamaFactory 仍然失败，就栽在这儿）。
CONTAINER_HOME = f"{WORKSPACE_TARGET}/.bench-home"

#: 逐个查文件列表的 PR 上限。一页 25 个，6 页 150 个够估比例了，
#: 再往下翻只是把配额烧在小数点后面。
DEFAULT_MAX_PRS = 150
#: 取样多少条 issue 判语言。
DEFAULT_ISSUE_SAMPLE = 100

#: 把近 2 年切成几段分别抽样。6 段 ≈ 每段 4 个月，够摊平"某类 PR 扎堆"的影响。
DEFAULT_BUCKETS = 6

#: 展开 PR 文件列表那个查询的超时。默认的 120 秒对大仓库不够 ——
#: PaddleOCR 一页 25 个 PR、每个展开 100 个文件，GitHub 要算一分多钟
#: （2026-09-08 实测超时）。这一个查询单独放宽，别的照旧。
PR_QUERY_TIMEOUT_S = 240

#: 实测阶段产生的问题都带这个前缀，重跑时按它清理。
MEASURE_TAG = "实测："

#: 第二段（容器实测）负责的字段。重跑第一段时要原样保住它们。
MEASURED_FIELDS = (
    "install_s",
    "test_s",
    "install_command",
    "test_exit_code",
    "tests_collected",
)

#: 安装和测试各自的容器墙钟上限。门槛是 120 / 180 秒，这里给到它的 1.7~2.5 倍：
#: 卡在 121 秒就杀掉的话，"这个仓库要装 8 分钟"和"要装 2 分钟"在数据里长得一样，
#: 而"超了多少"是选型时要看的。再往上加只是把时间花在已经出局的仓库上 ——
#: 20 个仓库每个多等 5 分钟就是多等一个半小时。
INSTALL_TIMEOUT_S = 300
TEST_TIMEOUT_S = 300

#: pip 走国内镜像。**不是可选项**：这台机器上直连 PyPI 会大面积超时
#: （`ReadTimeoutError` / `SSL: UNEXPECTED_EOF`，2026-09-08 实测），
#: 表现成"这个仓库装不上"，而它其实只是没拉到包。
#:
#: 后端自己也是这么配的（`pyproject.toml` 的 `[[tool.uv.index]]`，风险 R18），
#: 而且 E2-T3 建 env 镜像时同样会走它 —— 所以走镜像量出来的耗时才是
#: §8.3 那条 120 秒门槛该对照的数字。
PIP_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
PIP = f"python -m pip install -q --no-cache-dir -i {PIP_INDEX}"

#: 跑测试前一定装上的几个 pytest 插件。
#:
#: 不是凑数：很多仓库在 `pytest.ini` / `pyproject.toml` 的 `addopts` 里写死了
#: `--cov-report` 之类的参数，插件不在就直接 `error: unrecognized arguments`、
#: 退出码 4，一条用例都跑不了 —— python-pinyin 就是这样（2026-09-08 实测）。
#: 那样量出来的"测试耗时"是**失败所需的时间**，不是跑测试的时间，会误导选型。
#:
#: 只装最常被 addopts 引用的这几个，不是把所有插件都装一遍：
#: 装得越多越偏离"这个仓库开箱好不好跑"这个问题本身。
COMMON_TEST_PLUGINS = "pytest pytest-cov pytest-asyncio pytest-mock pytest-timeout"

#: 装**本体**的尝试顺序。第一条成功就停，`install_s` 记的就是它的耗时。
#:
#: 一律带 `--no-cache-dir`：容器里的 uid 在 `/etc/passwd` 里不存在，
#: pip 找不到可写的 HOME 来放缓存；而且不留缓存量出来的数字更接近
#: E2-T3 从零建 env 镜像时要付的开销。
#:
#: 只算本体是刻意的：§8.3 那条 120 秒门槛问的是"依赖体量"，
#: 也就是 E2-T3 建 env 镜像时要付的开销。测试插件（pytest-asyncio 之类）
#: 通常只有几秒，混进来只会让不同仓库之间不可比。
#:
#: `-e` 装成可编辑模式：真实评测里工作区是挂进去的，装成可编辑改动才立刻生效，
#: 和 E2-T3 建镜像的做法一致。
#: 装本体之前先把 setuptools / wheel 铺好。
#:
#: 只有 `setup.py`、没有 `pyproject.toml` 的老项目，在 pip 的构建隔离里会报
#: `Can not execute setup.py since setuptools is not available in the build
#: environment`（wechatpy 就是这样，2026-09-08 实测）。这是**基础镜像该有的东西**，
#: 不是这个仓库的依赖负担 —— E2-T3 建 bench-base 时本来也要装它。
#:
#: 它每个仓库都会跑，耗时是一个恒定的几秒，所以仓库之间仍然可比；
#: 拿它和 §8.3 的 120 秒门槛对照时记得这几秒是公共开销。
BOOTSTRAP = f"{PIP} setuptools wheel"

INSTALL_ATTEMPTS = (
    # 现代项目：pip 的构建隔离会自己把 build backend 拉下来
    f"{PIP} -e .",
    # 只有 setup.py 的老项目：构建隔离用的是**独立环境**，看不见我们刚装的
    # setuptools，所以要关掉隔离改用环境里的那份（wechatpy 这一类）
    f"{BOOTSTRAP} && {PIP} --no-build-isolation -e .",
    f"{BOOTSTRAP} && {PIP} -r requirements.txt",
)

#: 本体装完之后再尽力补测试依赖。**不计入 `install_s`**，失败也不影响结论。
#:
#: 全试一遍、不判断谁成功了：`pip install -e '.[不存在的extra]'` **不会失败**，
#: 它只打一条 warning 然后照常装本体、退出码 0（2026-09-08 实测）。
#: 所以"哪个 extra 真的存在"用退出码是判不出来的，也就不记这个了 ——
#: 记一个永远列出全部四个 extra 的字段，比不记更误导人。
TEST_DEP_ATTEMPTS = (
    f"{PIP} -e '.[test]'",
    f"{PIP} -e '.[tests]'",
    f"{PIP} -e '.[dev]'",
    f"{PIP} -e '.[testing]'",
    f"{PIP} -r requirements-dev.txt",
    f"{PIP} -r requirements/dev.txt",
    f"{PIP} -r requirements/test.txt",
    f"{PIP} -r test-requirements.txt",
)


# ══════════════════════════════════════════════════════════════
# 候选名单与存档
# ══════════════════════════════════════════════════════════════


def load_candidates(path: Path = CANDIDATES_FILE) -> list[tuple[str, str]]:
    """读候选名单，返回 `[(owner/repo, 组名)]`。"""
    if not path.exists():
        raise SystemExit(f"候选名单不存在：{path}")
    pairs: list[tuple[str, str]] = []
    group = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1]
            continue
        pairs.append((line, group))
    return pairs


def short(path: Path) -> str:
    """打印路径时优先用仓库相对形式。落在仓库外面（比如 --out 指到 /tmp）就打全路径 ——
    `relative_to` 对仓库外的路径会直接抛 ValueError，那会让一次跑完的探测在最后一行崩掉。
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def archive_path(stamp: str | None = None) -> Path:
    stamp = stamp or datetime.now(UTC).strftime("%Y-%m-%d")
    return SURVEY_DIR / f"repos-{stamp}.json"


def latest_archive() -> Path | None:
    files = sorted(SURVEY_DIR.glob("repos-*.json"))
    return files[-1] if files else None


def save(facts: Sequence[RepoFacts], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "window_days": CANDIDATE_WINDOW_DAYS,
        "measure_image": MEASURE_IMAGE,
        "build_limits": {"cpus": BUILD_LIMITS.cpus, "memory_mb": BUILD_LIMITS.memory_mb},
        "test_limits": {"cpus": TEST_LIMITS.cpus, "memory_mb": TEST_LIMITS.memory_mb},
        "repos": [f.to_json() for f in facts],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load(path: Path) -> list[RepoFacts]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [RepoFacts.from_json(r) for r in payload["repos"]]


# ══════════════════════════════════════════════════════════════
# 第一段：GitHub API
# ══════════════════════════════════════════════════════════════

#: 只数总数，不展开任何 PR。便宜到可以随便调。
_COUNT_QUERY = """
query($q: String!) {
  rateLimit { cost remaining limit }
  search(query: $q, type: ISSUE, first: 1) { issueCount }
}
"""

#: 宽口径那一遍：多取 `bodyText`，用来认"修复 #123"这种没有 closing 关键字的关联。
_PR_BROAD_QUERY = """
query($q: String!, $count: Int!) {
  rateLimit { cost remaining limit }
  search(query: $q, type: ISSUE, first: $count) {
    issueCount
    nodes {
      ... on PullRequest {
        number
        bodyText
        closingIssuesReferences(first: 1) { totalCount }
        files(first: 100) { nodes { path } }
      }
    }
  }
}
"""

#: 展开一批 PR 的文件列表。`files(first: 100)` 是这里最贵的一段，
#: 所以只对**关联了 issue** 的那批 PR 调它 —— 没关联 issue 的 PR 连题面都没有，
#: 展开它的文件列表纯属浪费配额。
_PR_QUERY = """
query($q: String!, $count: Int!) {
  rateLimit { cost remaining limit }
  search(query: $q, type: ISSUE, first: $count) {
    issueCount
    nodes {
      ... on PullRequest {
        number
        closingIssuesReferences(first: 1) { totalCount }
        files(first: 100) { nodes { path } }
      }
    }
  }
}
"""

_ISSUE_QUERY = """
query($owner: String!, $name: String!, $count: Int!) {
  rateLimit { cost remaining limit }
  repository(owner: $owner, name: $name) {
    issues(first: $count, orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes { number title body }
    }
  }
}
"""


def _window_buckets(buckets: int) -> list[tuple[str, str]]:
    """把近 2 年切成若干段，返回每段的 `(起, 止)` 日期。

    分段抽样而不是从最新的一路往下翻：翻页只会拿到最近那一批 PR，
    而"某段时间某类 PR 扎堆"是常态。nonebot2 就是活例子 ——
    它最近几百个关联 issue 的 merged PR 几乎全是插件商店的 registry 条目
    （2026-09-07 实测，跨四个季度抽 100 个只有 1 个带测试改动）。
    """
    end = datetime.now(UTC).date()
    span = CANDIDATE_WINDOW_DAYS / buckets
    edges = [end - timedelta(days=round(span * i)) for i in range(buckets + 1)]
    return [
        (edges[i + 1].strftime("%Y-%m-%d"), edges[i].strftime("%Y-%m-%d")) for i in range(buckets)
    ]


def _probe_pull_requests(facts: RepoFacts, *, max_prs: int, buckets: int) -> None:
    """量候选池深度：关联 issue 的 merged PR 数（精确）× 带测试且带源码的比例（抽样）。"""
    since = (datetime.now(UTC) - timedelta(days=CANDIDATE_WINDOW_DAYS)).strftime("%Y-%m-%d")
    base = f"repo:{facts.full_name} is:pr is:merged"

    # 两个总数都记：linked 是候选池的基数，merged 是给人看"这个仓库有多活跃"
    data, _ = graphql(_COUNT_QUERY, {"q": f"{base} merged:>={since}"})
    facts.merged_prs_2y = int(data["search"]["issueCount"])
    data, _ = graphql(_COUNT_QUERY, {"q": f"{base} linked:issue merged:>={since}"})
    facts.linked_prs_2y = int(data["search"]["issueCount"])

    per_bucket = max(1, max_prs // buckets)
    examined = candidates = 0
    for start, stop in _window_buckets(buckets):
        query = f"{base} linked:issue merged:{start}..{stop}"
        data, budget = graphql(
            _PR_QUERY, {"q": query, "count": per_bucket}, timeout_s=PR_QUERY_TIMEOUT_S
        )
        page_examined, page_candidates = count_pr_candidates(data["search"]["nodes"])
        examined += page_examined
        candidates += page_candidates
        if budget and budget.remaining < 200:
            facts.problems.append(f"GraphQL 配额只剩 {budget.remaining}，PR 抽样提前收工")
            break

    facts.prs_examined = examined or None
    facts.prs_candidate = candidates if examined else None

    # 再来一遍宽口径：不带 `linked:issue`，正文里提到 `#N` 也算关联。
    # 这一列不参与门槛，只回答"放宽关联口径的话候选池能大多少"——
    # 中文项目普遍只写"修复 #123"，严口径会把它们整批漏掉（2026-09-08 实测：
    # akshare 抽 60 个 PR，closing 关联 1 个、正文提到 #N 的 7 个）
    examined = candidates = 0
    for start, stop in _window_buckets(buckets):
        data, _ = graphql(
            _PR_BROAD_QUERY,
            {"q": f"{base} merged:{start}..{stop}", "count": per_bucket},
            timeout_s=PR_QUERY_TIMEOUT_S,
        )
        page_examined, page_candidates = count_pr_candidates(
            data["search"]["nodes"], allow_mention=True
        )
        examined += page_examined
        candidates += page_candidates
    facts.prs_examined_broad = examined or None
    facts.prs_candidate_broad = candidates if examined else None


def probe_repo(
    full_name: str, group: str, *, max_prs: int, issue_sample: int, buckets: int
) -> RepoFacts:
    """查一个仓库的 GitHub 侧事实。任何一段查不到都记进 `problems`，不抛出去。

    不抛的理由：20 个仓库跑一遍要几分钟，为了第 13 个仓库 404 就把前 12 个的
    结果连同烧掉的配额一起扔掉，太贵了。
    """
    facts = RepoFacts(full_name=full_name, group=group)
    owner, _, name = full_name.partition("/")

    try:
        meta = rest(f"repos/{full_name}")
        facts.license_spdx = ((meta.get("license") or {}).get("spdx_id")) or "无"
        facts.archived = bool(meta.get("archived"))
        facts.pushed_at = meta.get("pushed_at")
        facts.stars = meta.get("stargazers_count")
    except GitHubNotFoundError:
        facts.problems.append("仓库不存在或没有权限")
        return facts
    except GitHubError as exc:
        facts.problems.append(f"取仓库元数据失败：{exc}")
        return facts

    try:
        facts.python_ratio = python_ratio(rest(f"repos/{full_name}/languages"))
    except GitHubError as exc:
        facts.problems.append(f"取语言占比失败：{exc}")

    try:
        _probe_pull_requests(facts, max_prs=max_prs, buckets=buckets)
    except GitHubError as exc:
        facts.problems.append(f"查 PR 失败：{exc}")

    try:
        data, _ = graphql(_ISSUE_QUERY, {"owner": owner, "name": name, "count": issue_sample})
        nodes = ((data.get("repository") or {}).get("issues") or {}).get("nodes") or []
        sampled, zh, mixed = count_issue_languages(nodes)
        facts.issues_sampled = sampled or None
        facts.issues_zh = zh if sampled else None
        facts.issues_mixed = mixed if sampled else None
    except GitHubError as exc:
        facts.problems.append(f"查 issue 失败：{exc}")

    return facts


def cmd_probe(args: argparse.Namespace) -> int:
    if not gh_available():
        print("找不到 gh 命令。装好并 `gh auth login` 之后再跑", file=sys.stderr)
        return 1

    target = Path(args.out) if args.out else archive_path()
    existing = {f.full_name: f for f in (load(target) if target.exists() else [])}

    results: list[RepoFacts] = []
    for full_name, group in load_candidates(Path(args.candidates)):
        if args.only and full_name not in args.only:
            results.append(existing.get(full_name, RepoFacts(full_name=full_name, group=group)))
            continue
        print(f"查 {full_name} …", flush=True)
        facts = probe_repo(
            full_name,
            group,
            max_prs=args.max_prs,
            issue_sample=args.issue_sample,
            buckets=args.buckets,
        )
        # 已经量过容器那一段的话要保住，别被这次 probe 抹掉。
        # 字段清单写成常量，加字段时只有一处要改 —— 散在这里逐个赋值的话，
        # 迟早会加了字段忘了同步，表现是"重跑一次 probe，实测数据没了"
        previous = existing.get(full_name)
        if previous is not None:
            for field_name in MEASURED_FIELDS:
                setattr(facts, field_name, getattr(previous, field_name))
            facts.problems += [p for p in previous.problems if p.startswith(MEASURE_TAG)]
        results.append(facts)
        for problem in facts.problems:
            print(f"    ! {problem}", file=sys.stderr)

    save(results, target)
    print(f"\n已写出 {short(target)}")
    print_table(results)
    return 0


# ══════════════════════════════════════════════════════════════
# 第二段：容器实测
# ══════════════════════════════════════════════════════════════


#: 要带进容器的代理变量。大小写两套都带：curl 只认小写，requests / pip 两套都认，
#: 少哪一套都会有工具绕过代理直连（和 `AGENT_ENV_ALLOWLIST` 里那条注释同源）。
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")


def survey_env() -> dict[str, str]:
    """survey 容器里的环境变量。

    **刻意不走 `build_env()`**，因为要多给一个 `HOME`：容器里那个 uid 在
    `/etc/passwd` 里没有条目，docker 于是把 `HOME` 设成 `/`；非 root 装包时 pip
    会退回 user 安装、往 `/.local` 写，然后报
    `Permission denied: '/.local'`（2026-09-08 实测）。

    `HOME` 不在 `AGENT_ENV_ALLOWLIST` 里，那份白名单是给 **Agent 容器**防密钥泄漏用的；
    这里跑的是公开仓库的测试，没有那个风险面。但也正因为绕开了白名单，
    这里只拼**写死的常量 + 六个具名代理变量**，绝不从宿主机环境批量搬。
    """
    return {
        **DETERMINISM_ENV,
        # HOME 和 TMPDIR 都落到挂载进来的磁盘上：容器的 /tmp 是 tmpfs，占的是内存额度
        "HOME": CONTAINER_HOME,
        "TMPDIR": PIP_TMPDIR,
        # user 安装的可执行文件也要能找得到（pytest 插件的入口脚本装在这儿）
        "PATH": f"{CONTAINER_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        **proxy_env(),
    }


def proxy_env() -> dict[str, str]:
    """把宿主机的代理设置挑出来。

    容器**不继承**宿主机环境（`app.sandbox.container` 模块文档的"坑三"），
    而这台机器上 PyPI 必须走代理（`AGENTS.md` §10）。不带进去的话
    `pip install` 会一路超时，表现是"这个仓库装不上"—— 而它其实只是连不上网。
    这一条踩过：第一轮实测 20 个仓库全报装不上（2026-09-08）。
    """
    return {name: os.environ[name] for name in _PROXY_VARS if os.environ.get(name)}


def _container(
    workspace: Path,
    command: str,
    *,
    timeout_s: int,
    tail: int = 30,
    limits: ResourceLimits | None = None,
) -> ContainerResult:
    """在容器里跑一条 shell 命令，日志只留最后几行，**退出码如实带回来**。

    不能写成 `命令 | tail -30`：管道的退出码是**最后一条命令**的，也就是 `tail` 的，
    永远是 0。第一版就是这么写的，结果每个仓库都报"安装成功"，
    连 `pip install -e '.[test]'` 在没有这个 extra 的仓库上也算成功
    （2026-09-08 发现，那一轮 20 个仓库的数据整个作废）。

    `sh` 没有 `pipefail`，所以先把输出落到文件、存下退出码、再打尾巴、最后原样退出。
    """
    wrapped = (
        f"mkdir -p {PIP_TMPDIR} {CONTAINER_HOME}; "
        f"{{ {command} ; }} > /tmp/bench-out 2>&1; rc=$?; "
        f"tail -n {tail} /tmp/bench-out; exit $rc"
    )
    return run_in_container(
        ContainerSpec(
            image=MEASURE_IMAGE,
            command=["sh", "-lc", wrapped],
            timeout_s=timeout_s,
            stage=Stage.TEST,
            limits=limits or TEST_LIMITS,
            network=NetworkMode.BRIDGE,
            mounts=(BindMount.workspace(workspace),),
            workdir=WORKSPACE_TARGET,
            env=survey_env(),
            # 用默认的宿主机 uid 跑，**不要**改成 root。
            #
            # 容器规格里有 `cap_drop=ALL`，root 因此丢掉了 `CAP_DAC_OVERRIDE` ——
            # 也就是"无视文件权限"的那个特权。挂进来的工作区属于宿主机的 uid 1000、
            # 权限 755，容器里的 root 于是**写不进去**，`pip install -e .` 会以
            # `could not create 'xxx.egg-info': Permission denied` 收场
            # （2026-09-08 实测；手工 `docker run` 不加 cap_drop 就没这个问题，
            # 所以很容易误判成"这个仓库装不上"）。
            #
            # 跟着宿主机 uid 跑，工作区本来就是自己的，什么特权都不需要。
            run_id="survey",
        )
    )


def measure_repo(
    facts: RepoFacts,
    workdir: Path,
    *,
    install_timeout_s: int = INSTALL_TIMEOUT_S,
    test_timeout_s: int = TEST_TIMEOUT_S,
) -> None:
    """clone → 装依赖计时 → 跑全量测试计时。结果直接写回 `facts`。"""
    # 先清空上一轮的数字和问题。不清的话，这次失败会让报表里留着上次的耗时，
    # 看起来像"量到了"——那比没有数据更糟。
    #
    # 问题按 `MEASURE_TAG` 前缀清：只清实测阶段自己产生的那些，
    # probe 阶段记下的 API 问题要留着。按开头文字逐条匹配的写法每加一句新报错
    # 就要记得同步那张清单，迟早漏一条（已经漏过一次）
    facts.install_s = facts.test_s = None
    facts.install_command = None
    facts.test_exit_code = facts.tests_collected = None
    facts.problems = [p for p in facts.problems if not p.startswith(MEASURE_TAG)]

    checkout = workdir / facts.full_name.replace("/", "__")
    shutil.rmtree(checkout, ignore_errors=True)
    checkout.parent.mkdir(parents=True, exist_ok=True)

    try:
        run_git(
            [
                "clone",
                "--depth",
                "1",
                "--quiet",
                "--",
                f"https://github.com/{facts.full_name}.git",
                str(checkout),
            ],
            timeout_s=600,
        )
    except (GitError, subprocess.TimeoutExpired) as exc:
        facts.problems.append(f"{MEASURE_TAG}clone 失败：{str(exc)[:200]}")
        return

    # 第一步：装本体。这一段的耗时就是 install_s
    first_failure: str | None = None
    for attempt in INSTALL_ATTEMPTS:
        started = time.monotonic()
        try:
            result = _container(checkout, attempt, timeout_s=install_timeout_s, limits=BUILD_LIMITS)
        except SandboxError as exc:
            facts.problems.append(f"{MEASURE_TAG}起容器失败：{exc}")
            return
        elapsed = time.monotonic() - started
        if result.timed_out:
            facts.install_s = round(elapsed, 1)
            facts.install_command = f"{attempt}（超过 {install_timeout_s} 秒没装完）"
            facts.problems.append(f"{MEASURE_TAG}安装超时（>{install_timeout_s} 秒）")
            return
        if result.exit_code == 0:
            facts.install_s = round(elapsed, 1)
            facts.install_command = attempt
            break
        # 只留**第一次**尝试的原因：`pip install -e .` 是主路径，
        # 后面那几条 requirements 文件多半根本不存在，
        # 拿"没有 requirements.txt"顶包会把人指到完全错误的方向上
        if first_failure is None:
            first_failure = f"{attempt} → {_useful_tail(result.stdout)}"
    else:
        facts.problems.append(f"{MEASURE_TAG}装不上：{first_failure}")
        return

    # 第二步：尽力补测试依赖。不计时、失败也不管 —— 它只是为了让下一步的 pytest
    # 有机会真的跑起来。
    #
    # 八条**合成一次**容器运行（`;` 隔开，前面失败不影响后面）。原来一条起一个容器，
    # 光这一步每个仓库就要多起 8 次、多花半分钟，而这半分钟不进任何一个指标
    with contextlib.suppress(SandboxError):
        _container(
            checkout,
            "; ".join(TEST_DEP_ATTEMPTS),
            timeout_s=install_timeout_s,
            tail=3,
            limits=BUILD_LIMITS,
        )

    # 第三步：跑全量测试。
    #
    # 先按仓库根跑一遍；退出码 4（用法错误）或 5（一条都没收集到）时，
    # 退回显式指定 `tests/` 再试一次 —— 各家仓库的测试入口不统一，
    # 有的在 pytest 配置里限定了 testpaths，从仓库根跑反而一条都收不到。
    started = time.monotonic()
    test_run = None
    for target in ("", " tests"):
        try:
            test_run = _container(
                checkout,
                f"{PIP} {COMMON_TEST_PLUGINS} && python -m pytest -p no:cacheprovider -q{target}",
                timeout_s=test_timeout_s,
            )
        except SandboxError as exc:
            facts.problems.append(f"{MEASURE_TAG}跑测试时起容器失败：{exc}")
            return
        if test_run.exit_code not in (4, 5):
            break
    if test_run is None:
        return
    facts.test_s = round(time.monotonic() - started, 1)
    facts.test_exit_code = test_run.exit_code
    if test_run.timed_out:
        facts.problems.append(f"{MEASURE_TAG}测试超时（>{test_timeout_s} 秒）")
    facts.tests_collected = _collected(test_run.stdout)
    if not facts.tests_collected:
        why = _useful_tail(test_run.stdout)
        facts.problems.append(
            f"{MEASURE_TAG}没数出用例数（pytest 退出码 {test_run.exit_code}）：{why}"
        )


#: pip 每次都会打的噪声行，报错摘要里要跳过它们，否则看到的永远是"记得升级 pip"。
_PIP_NOISE = (
    "[notice]",
    "WARNING: You are using pip version",
    "You should consider upgrading",
    "hint: See above for details",
    "note: This error originates from a subprocess",
    "See above for output",
)


def _useful_tail(stdout: str, lines: int = 6, limit: int = 500) -> str:
    """从输出里挑出最有信息量的几行。

    只取一行会连着踩两次空：pip 失败时最后两行是"记得升级 pip"，
    再往上常常是 `hint: See above for details.` —— 三行都没说到底发生了什么
    （2026-09-08 排查 nonebot2 时就卡在这儿）。所以跳过已知噪声，取回**几行**拼起来。
    """
    useful = [
        ln.strip()
        for ln in stdout.strip().splitlines()
        if ln.strip() and not any(noise in ln for noise in _PIP_NOISE)
    ]
    return " | ".join(useful[-lines:])[:limit] if useful else "（没有输出）"


def _collected(stdout: str) -> int | None:
    """从 pytest 的结尾摘要里抠出用例总数。抠不到返回 None，不猜。"""
    import re

    counts = [int(n) for n in re.findall(r"(\d+)\s+(?:passed|failed|error|skipped)", stdout)]
    return sum(counts) or None


def cmd_measure(args: argparse.Namespace) -> int:
    source = Path(args.file) if args.file else latest_archive()
    if source is None or not source.exists():
        print("没有 probe 结果，先跑 `python -m cli.survey probe`", file=sys.stderr)
        return 1

    facts = load(source)
    workdir = Path(args.workdir)
    for record in facts:
        if args.only and record.full_name not in args.only:
            continue
        verdict = evaluate(record)
        # 已经被 API 那一段刷掉的就不用花几分钟装它了
        if not args.all and verdict.failed:
            reasons = "、".join(g.name for g in verdict.failed)
            print(f"跳过 {record.full_name}（{reasons} 没过）")
            continue
        if record.install_s is not None and not args.force:
            print(f"跳过 {record.full_name}（已量过，--force 可重来）")
            continue
        print(f"实测 {record.full_name} …", flush=True)
        measure_repo(
            record,
            workdir,
            install_timeout_s=args.install_timeout,
            test_timeout_s=args.test_timeout,
        )
        print(
            f"    安装 {record.install_s} 秒，测试 {record.test_s} 秒"
            f"（{record.tests_collected} 条用例，退出码 {record.test_exit_code}）"
        )
        for problem in record.problems:
            print(f"    ! {problem}", file=sys.stderr)
        save(facts, source)

    save(facts, source)
    print(f"\n已更新 {short(source)}")
    print_table(facts)
    return 0


# ══════════════════════════════════════════════════════════════
# 报表
# ══════════════════════════════════════════════════════════════


def print_table(facts: Sequence[RepoFacts]) -> None:
    verdicts = rank([evaluate(f) for f in facts])
    print()
    header = (
        f"{'仓库':34} {'结论':6} {'许可':13} {'Py':>5} "
        f"{'候选池':>7} {'宽口径':>7} {'中文':>6} {'安装':>7} {'测试':>7}"
    )
    print(header)
    print("-" * len(header))
    for v in verdicts:
        f = v.facts
        print(
            f"{f.full_name:34} {v.label:6} {(f.license_spdx or '—'):13} "
            f"{_pct(f.python_ratio):>5} {_num(f.estimated_candidates):>7} "
            f"{_num(f.estimated_candidates_broad):>7} "
            f"{_pct(f.zh_ratio):>6} {_secs(f.install_s):>7} {_secs(f.test_s):>7}"
        )
    print()
    for v in verdicts:
        if v.failed:
            reasons = "；".join(f"{g.name}={g.detail}" for g in v.failed)
            print(f"  ✗ {v.facts.full_name}：{reasons}")
        for problem in v.facts.problems:
            print(f"  ! {v.facts.full_name}：{problem}")

    chosen = [v for v in verdicts if v.passed]
    print(f"\n共 {len(verdicts)} 个候选，全部门槛过关 {len(chosen)} 个")
    if chosen:
        by_group: dict[str, int] = {}
        for v in chosen:
            by_group[v.facts.group] = by_group.get(v.facts.group, 0) + 1
        print(
            "  分组：" + "，".join(f"{g or '未分组'} {n} 个" for g, n in sorted(by_group.items()))
        )
        print(
            f"  门槛：Python≥{MIN_PYTHON_RATIO:.0%} · 宽松许可 · 近一年有推送 · "
            f"候选池≥{MIN_CANDIDATE_PRS} · 安装≤{MAX_INSTALL_S}s · 测试≤{MAX_TEST_S}s"
        )
    print(
        "\n  「候选池」按 §8.4 的严口径算（GitHub 认可的 linked:issue）；"
        "「宽口径」把正文里的 #N 提及也算上，只作诊断，不参与门槛。"
    )

    picks = shortlist(verdicts)
    if not picks:
        return
    print(f"\n定档候选（没有任何一条门槛判不过）：{len(picks)} 个")
    for v in picks:
        pending = "，容器实测未做" if v.unknown else ""
        print(
            f"  · {v.facts.full_name:34} 候选池 {v.facts.estimated_candidates or 0:>4}"
            f"  中文 {_pct(v.facts.zh_ratio):>4}  {v.facts.group}{pending}"
        )
    total = sum(v.facts.estimated_candidates or 0 for v in picks)
    domestic = [v for v in picks if "国产" in v.facts.group]
    print(f"  合计候选池约 {total} 个；国产/中文社区 {len(domestic)} 个仓库")


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _num(value: int | None) -> str:
    return "—" if value is None else str(value)


def _secs(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}s"


def cmd_report(args: argparse.Namespace) -> int:
    source = Path(args.file) if args.file else latest_archive()
    if source is None or not source.exists():
        print("没有实测数据，先跑 `python -m cli.survey probe`", file=sys.stderr)
        return 1
    print(f"数据来自 {short(source)}")
    print_table(load(source))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cli.survey", description="仓库选型实测与打分（E8-T1，§8.3）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser("probe", help="第一段：查 GitHub（许可证、候选池深度、中文比例）")
    p_probe.add_argument("--candidates", default=str(CANDIDATES_FILE))
    p_probe.add_argument("--out", help=f"默认 {SURVEY_DIR.name}/repos-<日期>.json")
    p_probe.add_argument("--only", action="append", help="只查这几个仓库")
    p_probe.add_argument(
        "--max-prs", type=int, default=DEFAULT_MAX_PRS, help="逐个查文件的 PR 上限"
    )
    p_probe.add_argument("--issue-sample", type=int, default=DEFAULT_ISSUE_SAMPLE)
    p_probe.add_argument(
        "--buckets", type=int, default=DEFAULT_BUCKETS, help="把近 2 年切成几段分别抽样"
    )
    p_probe.set_defaults(func=cmd_probe)

    p_measure = sub.add_parser("measure", help="第二段：容器里实测安装与测试耗时（要 Docker）")
    p_measure.add_argument("--file", help="默认取最新一份 repos-*.json")
    p_measure.add_argument("--workdir", default=str(REPO_ROOT / "var" / "survey"))
    p_measure.add_argument("--only", action="append", help="只测这几个仓库")
    p_measure.add_argument("--all", action="store_true", help="连 API 段没过关的也测")
    p_measure.add_argument("--force", action="store_true", help="已经量过的也重来")
    p_measure.add_argument("--install-timeout", type=int, default=INSTALL_TIMEOUT_S)
    p_measure.add_argument("--test-timeout", type=int, default=TEST_TIMEOUT_S)
    p_measure.set_defaults(func=cmd_measure)

    p_report = sub.add_parser("report", help="打分表")
    p_report.add_argument("--file", help="默认取最新一份 repos-*.json")
    p_report.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "load_candidates", "main", "measure_repo", "probe_repo"]
