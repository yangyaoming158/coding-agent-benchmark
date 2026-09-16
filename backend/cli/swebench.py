"""SWE-bench Verified 官方题导入的命令行（E1-T7，`03-benchmark-spec.md` §8.6）。

    python -m cli.swebench fetch               # 拉官方数据集 500 行 → var/cache/swebench/
    python -m cli.swebench screen              # 离线筛：判定引擎认不认、题面合不合规（漏斗第一层）
    python -m cli.swebench sample              # 固定种子分层抽样，名单写进 datasets/swebench/
    python -m cli.swebench estimate            # 只读镜像 manifest，算抽中的题要下多少 GB
    python -m cli.swebench warm                # 预热镜像站：每层碰几秒，让它先去 Docker Hub 缓存
    python -m cli.swebench pull                # 拉官方镜像 + 探 python / pytest 版本
    python -m cli.swebench fetch-blobs         # 用 Windows 的 curl.exe 把镜像的层下到 D 盘
    python -m cli.swebench load                # 把下好的层拼成 OCI 布局 docker load，再探测登记
    python -m cli.swebench build               # 按官方配方本机建镜像（依赖走清华源）
    python -m cli.swebench mirror              # 备好 git 镜像（按 base_commit 浅拉，不 clone 全史）
    python -m cli.swebench import              # 组装题目 + 环境规格入库
    python -m cli.swebench review-csv          # 验证后停在 REVIEW_REQUIRED 的题 → 终审对照表
    python -m cli.swebench report [--save]     # 导入漏斗（AC 8）

顺序就是上面这个顺序。然后：

    python -m cli.validate run --dataset swebench-verified-subset --scope declared
    make dataset-stage DATASET=swebench-verified-subset
    make dataset-gate SLUG=swebench-verified-subset && make worker
    make dataset-publish SLUG=swebench-verified-subset

## 纯逻辑在 `app.benchmark.swebench_import`，这里只管落盘、联网、起容器、写库

字段映射、抽样、离线筛、组装全是纯函数，测试里不联网不起库就能验。
本文件负责的都是有副作用的事：HTTP、docker、git、数据库。

## 三份本地状态，都在 `var/cache/swebench/`（不进仓库）

- `verified.jsonl` + `verified.meta.json`：官方数据集原文和它的指纹（行数、sha256、HF 修订号）
- `images.json`：每道抽中的题镜像拉没拉到、探到的 python / pytest 版本、镜像 digest
- `mirrors.json`：每个仓库的 git 镜像备好了哪些 base_commit

抽样名单**进仓库**（`datasets/swebench/sample-seed<seed>-n<n>.json`，KB 级）：
它是"这 50 道是怎么来的"的可复核证据 —— 虽然同一种子重算就能得到同一份，
但把结果也提交，review 的人不用跑代码就能看到名单。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

import httpx
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.benchmark.swebench_import import (
    DATASET_ID,
    DEFAULT_SAMPLE_SIZE,
    DEFAULT_SEED,
    HF_DATASET,
    OFFICIAL_ENV_PYTHON,
    OFFICIAL_TESTBED,
    Funnel,
    Screened,
    SwebenchImportError,
    VerifiedInstance,
    build_task,
    built_environment,
    fallback_environment,
    load_instances,
    official_environment,
    official_image,
    render_funnel,
    screen_all,
    stratified_sample,
)
from app.domain.enums import ImageBuildStatus, TaskValidationState
from app.infrastructure.config import REPO_ROOT, get_settings
from app.infrastructure.db import create_db_engine, create_session_factory, session_scope
from app.infrastructure.models.benchmark import BenchmarkTask, EnvironmentSpec
from app.sandbox.container import (
    ImageNotFoundError,
    default_container_user,
    get_docker_client,
    inspect_image,
)
from app.sandbox.git_cli import GitError, run_git
from app.sandbox.mirror import MirrorManager, pin_archive_attributes
from app.storage import create_artifact_store, key_from_uri
from cli.queue import upsert_task

CACHE_DIR = REPO_ROOT / "var" / "cache" / "swebench"
ROWS_FILE = CACHE_DIR / "verified.jsonl"
META_FILE = CACHE_DIR / "verified.meta.json"
IMAGES_FILE = CACHE_DIR / "images.json"
MIRRORS_FILE = CACHE_DIR / "mirrors.json"
SAMPLE_DIR = REPO_ROOT / "datasets" / "swebench"

#: HF 的 datasets-server：分页给 JSON 行，不用装 pyarrow 读 parquet。
#: 一页最多 100 行；实测这台机器过代理时大页容易超时，默认 50。
HF_ROWS_URL = "https://datasets-server.huggingface.co/rows"
HF_INFO_URL = f"https://huggingface.co/api/datasets/{HF_DATASET}"
HF_PAGE_SIZE = 50
HTTP_RETRIES = 5
HTTP_BACKOFF_S = (5, 15, 30, 60, 90)

#: Docker Hub 的 registry API，`estimate` 只读 manifest 不拉层。
REGISTRY_URL = "https://registry-1.docker.io"
REGISTRY_AUTH_URL = "https://auth.docker.io/token"

#: 国内镜像站，`warm` 默认预热它（dockerd 的 `registry-mirrors` 里也是它，2026-09-16 起）。
#: **直连，不走代理**：实测走代理到它是 0 B/s，直连缓存命中时 1.6–2 MB/s。
#: 它是"先有人要才去 Docker Hub 缓存"的：一个层第一次被要时只给几 KB/s 甚至 0，
#: 等它自己拉完了再要才快 —— 所以要先预热再 `docker pull`。
DEFAULT_MIRROR_URL = "https://docker.1ms.run"
#: 预热时每个层碰多少秒。目的只是触发镜像站去上游拉，不是下完。
WARM_SECONDS_PER_LAYER = 6.0
#: 碰的这几秒里平均到这个速度就算"已经热了"。实测缓存命中时 350 KB/s–2 MB/s，
#: 没命中（它正去上游拉）时 0–11 KB/s，中间隔着一个数量级，卡 100 KB/s 分得开。
WARM_HOT_BYTES_PER_S = 100 * 1024
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

#: Windows 侧下载（`fetch-blobs`）。
#:
#: 2026-09-16 实测：同一个 VPN 代理，从 Windows 走 127.0.0.1:10808 是 14–15 MB/s，
#: 从 WSL 走 172.30.80.1:10808 只有 0.08–1.2 MB/s，关掉 vEthernet 的 LSO 也没用 ——
#: 瓶颈是 WSL 到宿主那一跳。`curl.exe` 是 Windows 自带的 curl，从 WSL 里能直接调，
#: 它走的是 Windows 的网络栈；下到 Windows 的盘上，WSL 从 `/mnt/<盘>` 读回来校验和装载。
#: C 盘没空间（2026-09-16 剩 34 GB），放 D 盘；D 盘根目录 Windows 用户也写不了，放 Documents 下。
WINDOWS_PROXY = "http://127.0.0.1:10808"
WINDOWS_BLOB_DIR = r"D:\Documents\swebench-blobs"
WSL_BLOB_DIR = Path("/mnt/d/Documents/swebench-blobs")
#: 每个层最多等多久（秒）。最大的层 2.4 GB，按 5 MB/s 也要 8 分钟。
BLOB_TIMEOUT_S = 1800

#: 拉一个镜像最多试几次。docker 会把已经拉完的层留着，重试只补没拉完的。
PULL_RETRIES = 3
#: 一次拉取的进度事件之间最多等多久没动静就算卡死。这台机器的代理会卡住整条连接
#: （2026-09-15 实测：`docker pull` 26 分钟进账 1 KB/s），不掐掉就一直挂着。
PULL_STALL_S = 300

#: 探镜像用的脚本：python / pytest 版本、`/testbed` 停在哪个 commit、有多少构建产物。
#:
#: `/testbed` 的 HEAD **不是** `base_commit`：2025 年后的官方 harness 建镜像时会在
#: base 之上再提交一个叫 "SWE-bench" 的 commit（实测 `psf__requests-2317`：只改文件模式，
#: `chmod -R 777` 的结果，一行内容都没变）。所以对得上的判据是"base 是 HEAD 或 HEAD 的父提交"。
#: 官方环境最老是 python 3.6（scikit-learn 0.2x、astropy 3.x），所以这段脚本不能用 3.7 才有的
#: `subprocess.run(capture_output=)`（2026-09-16 踩过：8 道题镜像建好了、探测却报 TypeError）。
PROBE_SCRIPT = f"""\
import json, subprocess, sys
info = {{"python": "%d.%d.%d" % sys.version_info[:3]}}
try:
    import pytest
    info["pytest"] = pytest.__version__
except Exception as exc:
    info["pytest"] = None
    info["pytest_error"] = str(exc)
git = ["git", "-c", "safe.directory=*", "-C", {OFFICIAL_TESTBED!r}]
pipes = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
head = subprocess.run([*git, "rev-parse", "HEAD"], **pipes)
info["testbed_head"] = head.stdout.strip() if head.returncode == 0 else None
parent = subprocess.run([*git, "rev-parse", "HEAD~1"], **pipes)
info["testbed_parent"] = parent.stdout.strip() if parent.returncode == 0 else None
ignored = subprocess.run(
    [*git, "ls-files", "-z", "--others", "--ignored", "--exclude-standard"], **pipes
)
paths = [p for p in ignored.stdout.split("\\0") if p]
info["ignored_files"] = len(paths) if ignored.returncode == 0 else None
print(json.dumps(info))
"""
PROBE_TIMEOUT_S = 120


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ══════════════════════════════════════════════════════════════
# HTTP
# ══════════════════════════════════════════════════════════════


def http_proxy_from_env() -> str | None:
    """这台机器出网靠 `HTTPS_PROXY`（http:// 形式）。

    不让 httpx 自己读环境：它会顺手读到 `ALL_PROXY=socks5h://…`，没装 socksio 就在
    建客户端时 ImportError（`app.infrastructure.llm` 那边踩过）。只认 http(s) 的那两个。
    """
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.environ.get(name)
        if value and value.startswith(("http://", "https://")):
            return value
    return None


def make_client(timeout_s: float = 120.0, proxy: str | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=timeout_s,
        trust_env=False,
        proxy=proxy or http_proxy_from_env(),
        follow_redirects=True,
    )


def _get_json(client: httpx.Client, url: str, **kwargs: Any) -> Any:
    """GET 一个 JSON，网络抖动就退避重试。过代理时超时和 SSL 断连都很常见。"""
    last = ""
    for attempt in range(HTTP_RETRIES + 1):
        try:
            response = client.get(url, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            if attempt >= HTTP_RETRIES:
                break
            wait = HTTP_BACKOFF_S[min(attempt, len(HTTP_BACKOFF_S) - 1)]
            print(f"    请求失败（{last[:120]}），{wait} 秒后重试 {attempt + 1}/{HTTP_RETRIES}")
            time.sleep(wait)
    raise SwebenchImportError(f"GET {url} 失败：{last}")


# ══════════════════════════════════════════════════════════════
# fetch
# ══════════════════════════════════════════════════════════════


def fetch_rows(client: httpx.Client, *, page_size: int = HF_PAGE_SIZE) -> list[dict[str, Any]]:
    """把整个 test split 分页拉下来。任何一格被截断（`truncated_cells` 非空）就报错 ——
    截断的补丁打不上去，静默收下会在很后面才露馅。"""
    rows: list[dict[str, Any]] = []
    offset, total = 0, None
    while total is None or offset < total:
        payload = _get_json(
            client,
            HF_ROWS_URL,
            params={
                "dataset": HF_DATASET,
                "config": "default",
                "split": "test",
                "offset": offset,
                "length": page_size,
            },
        )
        total = int(payload["num_rows_total"])
        page = payload["rows"]
        for item in page:
            if item.get("truncated_cells"):
                raise SwebenchImportError(
                    f"{item['row'].get('instance_id')} 有被截断的字段：{item['truncated_cells']}"
                )
            rows.append(item["row"])
        print(f"  {offset + len(page):>4}/{total}")
        if not page:
            raise SwebenchImportError(f"offset={offset} 返回了空页，total={total}")
        offset += len(page)
    return rows


def _hf_revision(client: httpx.Client) -> str | None:
    """数据集仓库当前的 git 修订号，进 meta 记一笔；拿不到不算失败。"""
    try:
        info = _get_json(client, HF_INFO_URL)
    except SwebenchImportError:
        return None
    sha = info.get("sha")
    return str(sha) if sha else None


def write_rows(rows: Sequence[Mapping[str, Any]], *, revision: str | None) -> dict[str, Any]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(dict(row), ensure_ascii=False) + "\n" for row in rows)
    ROWS_FILE.write_text(text, encoding="utf-8")
    meta = {
        "dataset": HF_DATASET,
        "split": "test",
        "num_rows": len(rows),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "hf_revision": revision,
        "fetched_at": _now_iso(),
        "source": HF_ROWS_URL,
    }
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return meta


def cmd_fetch(args: argparse.Namespace) -> int:
    if ROWS_FILE.exists() and not args.refresh:
        meta = json.loads(META_FILE.read_text(encoding="utf-8")) if META_FILE.exists() else {}
        rows, when = meta.get("num_rows", "?"), meta.get("fetched_at", "?")
        print(f"已有 {ROWS_FILE}（{rows} 行，取于 {when}）")
        print("要重拉加 --refresh")
        return 0
    print(f"从 {HF_ROWS_URL} 拉 {HF_DATASET}（每页 {args.page_size} 行）")
    with make_client(timeout_s=args.timeout) as client:
        rows = fetch_rows(client, page_size=args.page_size)
        revision = _hf_revision(client)
    meta = write_rows(rows, revision=revision)
    digest = meta["sha256"][:12]
    print(f"写入 {ROWS_FILE}：{meta['num_rows']} 行，sha256 {digest}…，HF 修订 {revision}")
    return 0


# ══════════════════════════════════════════════════════════════
# screen
# ══════════════════════════════════════════════════════════════


def _load_all() -> list[VerifiedInstance]:
    if not ROWS_FILE.exists():
        raise SystemExit(f"没有 {ROWS_FILE}，先跑 `python -m cli.swebench fetch`")
    return load_instances(ROWS_FILE)


def _pool(screened: Sequence[Screened]) -> list[VerifiedInstance]:
    return [item.instance for item in screened if item.ok]


def cmd_screen(args: argparse.Namespace) -> int:
    instances = _load_all()
    screened = screen_all(instances)
    buckets = Counter(item.bucket for item in screened)
    print(f"官方题 {len(instances)} 道，离线筛：\n")
    for bucket, count in sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {bucket:<26} {count}")
    per_repo: Counter[str] = Counter(item.instance.repo for item in screened if item.ok)
    print("\n抽样池（按仓库）：")
    for repo, count in sorted(per_repo.items()):
        print(f"  {repo:<28} {count}")
    if args.verbose:
        print("\n被筛掉的（不含整仓库排除的那两家）：")
        for item in screened:
            if item.ok or item.instance.repo not in {i.instance.repo for i in screened if i.ok}:
                continue
            print(f"  {item.instance.instance_id:<36} {item.bucket:<26} {item.detail[:80]}")
    return 0


# ══════════════════════════════════════════════════════════════
# sample
# ══════════════════════════════════════════════════════════════


def sample_path(seed: int, size: int) -> Path:
    return SAMPLE_DIR / f"sample-seed{seed}-n{size}.json"


def load_sample(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"没有抽样名单 {path}，先跑 `python -m cli.swebench sample`")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _chosen_instances(
    sample: Mapping[str, Any], instances: Sequence[VerifiedInstance]
) -> list[VerifiedInstance]:
    by_id = {instance.instance_id: instance for instance in instances}
    missing = [instance_id for instance_id in sample["chosen"] if instance_id not in by_id]
    if missing:
        raise SystemExit(f"抽样名单里的题在数据集里找不到：{missing[:5]}（数据集换版本了？）")
    return [by_id[instance_id] for instance_id in sample["chosen"]]


def cmd_sample(args: argparse.Namespace) -> int:
    screened = screen_all(_load_all())
    pool = _pool(screened)
    sample = stratified_sample(pool, size=args.n, seed=args.seed)
    path = sample_path(args.seed, args.n)

    if args.check:
        # 复核：文件里的名单和现在重算的一模一样才算数（AC 2 的可复现）
        stored = load_sample(path)
        same = stored == sample.to_json()
        print(f"{path}：{'和重算结果逐字相同' if same else '和重算结果不同！'}")
        return 0 if same else 1

    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sample.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"池 {sample.pool_size} 道（摘要 {sample.pool_digest[:12]}…）")
    print(f"种子 {sample.seed}，抽 {sample.size} 道：\n")
    for repo, quota in sorted(sample.quotas.items()):
        print(f"  {repo:<28} {quota:>2}  备选 {len(sample.replacements[repo])}")
    print(f"\n名单写入 {path}")
    return 0


def _sample_and_instances(
    args: argparse.Namespace,
) -> tuple[dict[str, Any], list[VerifiedInstance]]:
    sample = load_sample(sample_path(args.seed, args.n))
    return sample, _chosen_instances(sample, _load_all())


# ══════════════════════════════════════════════════════════════
# estimate
# ══════════════════════════════════════════════════════════════


_AUTH_PARAM = re.compile(r'(\w+)="([^"]*)"')


def _registry_token(client: httpx.Client, registry: str, repository: str) -> str:
    """按 registry 协议拿匿名 pull 令牌：先无凭证请求 manifest，从 401 的
    `WWW-Authenticate` 里读 realm / service，再去那里换 token。

    Docker Hub（realm 是 auth.docker.io）和镜像站（各家 realm 不同）都走这一套，
    不用写死任何一家的地址。返回的字段有的叫 `token` 有的叫 `access_token`，两个都认。
    """
    probe = client.get(f"{registry}/v2/{repository}/manifests/latest")
    if probe.status_code != 401:
        return ""  # 不要认证
    challenge = dict(_AUTH_PARAM.findall(probe.headers.get("www-authenticate", "")))
    realm = challenge.get("realm") or REGISTRY_AUTH_URL
    params = {"scope": f"repository:{repository}:pull"}
    if challenge.get("service"):
        params["service"] = challenge["service"]
    payload = _get_json(client, realm, params=params)
    return str(payload.get("token") or payload.get("access_token") or "")


def _auth_headers(token: str, accept: str) -> dict[str, str]:
    headers = {"Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def image_layers(
    client: httpx.Client, image: str, *, registry: str = REGISTRY_URL
) -> list[tuple[str, int]]:
    """读一个镜像 amd64 版的 manifest，返回 `[(层 digest, 字节数)]`。只读元数据，不拉层。"""
    repository, _, tag = image.partition(":")
    headers = _auth_headers(_registry_token(client, registry, repository), MANIFEST_ACCEPT)
    manifest = _get_json(client, f"{registry}/v2/{repository}/manifests/{tag}", headers=headers)
    if "manifests" in manifest:  # 多架构索引：挑 linux/amd64 那一份
        for entry in manifest["manifests"]:
            platform = entry.get("platform") or {}
            if platform.get("architecture") == "amd64" and platform.get("os") == "linux":
                manifest = _get_json(
                    client,
                    f"{registry}/v2/{repository}/manifests/{entry['digest']}",
                    headers=headers,
                )
                break
        else:
            raise SwebenchImportError(f"{image} 没有 linux/amd64 的 manifest")
    return [(str(layer["digest"]), int(layer["size"])) for layer in manifest.get("layers", [])]


def cmd_estimate(args: argparse.Namespace) -> int:
    _, chosen = _sample_and_instances(args)
    seen: dict[str, int] = {}
    per_image: list[tuple[str, int, int]] = []
    with make_client(timeout_s=args.timeout) as client:
        for instance in chosen:
            image = official_image(instance.instance_id)
            layers = image_layers(client, image)
            new = sum(size for digest, size in layers if digest not in seen)
            per_image.append((instance.instance_id, sum(s for _, s in layers), new))
            seen.update(dict(layers))
            whole = per_image[-1][1] / 1e9
            print(f"  {instance.instance_id:<36} 镜像 {whole:5.2f} GB，新增 {new / 1e9:5.2f} GB")
    total = sum(seen.values())
    print(
        f"\n{len(chosen)} 个镜像，去重后共 {len(seen)} 层、{total / 1e9:.1f} GB（压缩后的下载量）"
    )
    return 0


# ══════════════════════════════════════════════════════════════
# warm：预热镜像站
# ══════════════════════════════════════════════════════════════


def touch_blob(
    client: httpx.Client, registry: str, repository: str, digest: str, token: str, *, seconds: float
) -> int:
    """把一个层"要"上 `seconds` 秒就断开，返回这段时间收到了多少字节。

    收得多说明镜像站已经缓存了；收得少或 0 说明它正去上游拉，过一会再来就快了。
    连不上、超时都按 0 字节算 —— 预热是尽力而为，一个层碰不到不该让整轮停下。
    """
    deadline = time.monotonic() + seconds
    received = 0
    try:
        with client.stream(
            "GET",
            f"{registry}/v2/{repository}/blobs/{digest}",
            headers=_auth_headers(token, "application/octet-stream"),
        ) as response:
            for chunk in response.iter_bytes(chunk_size=256 * 1024):
                received += len(chunk)
                if time.monotonic() > deadline:
                    break
    except httpx.HTTPError:
        pass
    return received


def cmd_warm(args: argparse.Namespace) -> int:
    _, chosen = _sample_and_instances(args)
    if args.only:
        wanted = set(args.only)
        chosen = [instance for instance in chosen if instance.instance_id in wanted]
    registry = args.mirror.rstrip("/")
    seen: set[str] = set()
    hot = cold = 0
    # 直连镜像站：proxy=None 且 trust_env=False，环境里的代理变量一概不读
    with httpx.Client(timeout=httpx.Timeout(args.timeout), trust_env=False) as client:
        for instance in _pull_order(chosen)[: args.limit]:
            image = official_image(instance.instance_id)
            repository, _, _tag = image.partition(":")
            try:
                layers = image_layers(client, image, registry=registry)
                token = _registry_token(client, registry, repository)
            except (SwebenchImportError, httpx.HTTPError) as exc:
                print(f"  ✗ {instance.instance_id:<36} manifest 拿不到：{str(exc)[:80]}")
                continue
            fresh = [(d, size) for d, size in layers if d not in seen and size > 0]
            seen.update(d for d, _ in fresh)
            marks = []
            for digest, size in fresh:
                got = touch_blob(client, registry, repository, digest, token, seconds=args.seconds)
                is_hot = got >= size or got >= WARM_HOT_BYTES_PER_S * args.seconds
                hot += int(is_hot)
                cold += int(not is_hot)
                marks.append(f"{'●' if is_hot else '○'}{size / 1e6:.0f}M")
            print(f"  {instance.instance_id:<36} {' '.join(marks) or '（层都碰过了）'}")
    print(f"\n碰了 {hot + cold} 层：热 {hot}，冷 {cold}")
    print("（●=缓存命中 ○=镜像站还在去上游拉，过几分钟再跑一遍）")
    return 0 if cold == 0 else 1


# ══════════════════════════════════════════════════════════════
# pull
# ══════════════════════════════════════════════════════════════


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _image_present(client: Any, image: str) -> bool:
    try:
        inspect_image(image, client=client)
    except ImageNotFoundError:
        return False
    return True


def pull_image(client: Any, image: str, *, stall_s: int = PULL_STALL_S) -> None:
    """拉一个镜像，`stall_s` 秒内下载量没有增长就当卡死抛错，让上层重试。

    走底层 API 拿事件流而不是 `images.pull()`：后者一口气等到结束，卡住的时候
    从外面看不出是慢还是死。

    卡死有两种样子，要分开对付：

    - **完全没动静**：流上一个字节都不来。请求上设了 `stall_s` 的读超时，到点抛
      `ReadTimeout`，冒出去由调用方重试。
    - **细水长流**：事件一直来，但每层的 `progressDetail.current` 不涨（2026-09-15 实测
      1 KB/s 挂了 26 分钟）。事件不断所以读超时抓不到，只能自己看字节数有没有涨。
    """
    repository, _, tag = image.partition(":")
    # 不用 `client.api.pull()`：它把读超时写死成 None（源码里 `timeout=None`），
    # 流上没动静时会永远等下去 —— 2026-09-15 实测挂了 15 分钟一个事件都没有。
    # 这里用同一个 SDK 的底层请求把读超时设成 `stall_s`，其余和 `pull()` 一模一样。
    api = client.api
    response = api._post(
        api._url("/images/create"),
        params={"fromImage": repository, "tag": tag or "latest"},
        stream=True,
        timeout=stall_s,
    )
    stream = api._stream_helper(response, decode=True)
    progress: dict[str, int] = {}
    last_growth = time.monotonic()
    last_print = 0.0
    for event in stream:
        now = time.monotonic()
        if "error" in event:
            raise SwebenchImportError(f"docker pull {image}：{event['error']}")
        layer = str(event.get("id") or "")
        current = (event.get("progressDetail") or {}).get("current")
        if layer and isinstance(current, int) and current > progress.get(layer, 0):
            progress[layer] = current
            last_growth = now
        elif event.get("status") in ("Pull complete", "Download complete", "Already exists"):
            # 一层结束也是进展，哪怕它的字节数早就到顶了
            last_growth = now
        if now - last_growth > stall_s:
            response.close()
            raise SwebenchImportError(f"docker pull {image} 超过 {stall_s} 秒下载量没有增长")
        if now - last_print > 30:
            done_mb = sum(progress.values()) / 1e6
            status = str(event.get("status") or "")
            print(f"      … 已下载 {done_mb:,.0f} MB，{len(progress)} 层有进度，{status}")
            last_print = now


def probe_image(client: Any, image: str) -> dict[str, Any]:
    """在镜像里跑 `PROBE_SCRIPT`，拿 python / pytest 版本和 `/testbed` 的 HEAD。"""
    container = client.containers.create(
        image,
        [OFFICIAL_ENV_PYTHON, "-c", PROBE_SCRIPT],
        network_mode="none",
        user=default_container_user(),
        detach=True,
    )
    try:
        container.start()
        result = container.wait(timeout=PROBE_TIMEOUT_S)
        stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
        stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
    finally:
        container.remove(force=True)
    if int(result.get("StatusCode", 1)) != 0:
        raise SwebenchImportError(f"探测脚本退出码 {result.get('StatusCode')}：{stderr[-300:]}")
    line = stdout.strip().splitlines()[-1] if stdout.strip() else "{}"
    info: dict[str, Any] = json.loads(line)
    return info


#: 拉取顺序：env 层小的仓库先拉，验证能早点开始；matplotlib 一个桶 2.4 GB（带 texlive）、
#: 7 个桶占了 38.9 GB 里的一半，放最后。名单外的仓库排在 matplotlib 前面。
PULL_PRIORITY: Mapping[str, int] = {
    "pallets/flask": 0,
    "pylint-dev/pylint": 1,
    "pytest-dev/pytest": 2,
    "sphinx-doc/sphinx": 3,
    "mwaskom/seaborn": 4,
    "astropy/astropy": 5,
    "scikit-learn/scikit-learn": 6,
    "pydata/xarray": 7,
    "matplotlib/matplotlib": 99,
}


def _pull_order(chosen: Iterable[VerifiedInstance]) -> list[VerifiedInstance]:
    """按 `PULL_PRIORITY` 排仓库，同一个 env 桶的挨在一起：它们共享 env 层，docker 只下一遍。"""
    return sorted(
        chosen,
        key=lambda i: (
            PULL_PRIORITY.get(i.repo, 50),
            i.repo,
            i.environment_setup_commit,
            i.instance_id,
        ),
    )


def cmd_pull(args: argparse.Namespace) -> int:
    _, chosen = _sample_and_instances(args)
    if args.only:
        wanted = set(args.only)
        unknown = sorted(wanted - {instance.instance_id for instance in chosen})
        if unknown:
            # 名单外的题不拉：拉了也进不了数据集（import 只认名单里的）
            print(f"  ⚠ 不在抽样名单里，忽略：{'、'.join(unknown)}")
        chosen = [instance for instance in chosen if instance.instance_id in wanted]
    status = _read_json(IMAGES_FILE)
    client = get_docker_client()
    done = failed = skipped = consecutive_failures = 0

    for instance in _pull_order(chosen)[: args.limit]:
        if consecutive_failures >= args.give_up_after:
            # 连着几个都拉不动，多半是代理整个挂了，一个个试下去只是每个再白等 15 分钟。
            # 停下来非零退出，外面的重试循环睡一会再来（见 AGENTS.md 第 12 节）
            print(f"  连续 {consecutive_failures} 个失败，这一轮先停；稍后重跑接着拉")
            break
        image = official_image(instance.instance_id)
        record = dict(status.get(instance.instance_id) or {})
        if record.get("status") == "pulled" and not args.force and _image_present(client, image):
            skipped += 1
            continue
        if args.dry_run:
            print(f"  会拉 {image}")
            continue

        print(f"  {instance.instance_id}：{image}")
        error = ""
        for attempt in range(1, PULL_RETRIES + 1):
            try:
                if not _image_present(client, image) or args.force:
                    pull_image(client, image)
                info = inspect_image(image, client=client)
                probe = probe_image(client, image)
                break
            except Exception as exc:  # 网络、docker、探测脚本，任何一种都要重试
                error = f"{type(exc).__name__}: {str(exc)[:200]}"
                print(f"    第 {attempt} 次失败：{error}")
                time.sleep(10)
        else:
            failed += 1
            consecutive_failures += 1
            status[instance.instance_id] = {
                **record,
                "image": image,
                "status": "failed",
                "error": error,
                "attempted_at": _now_iso(),
            }
            _write_json(IMAGES_FILE, status)
            continue
        consecutive_failures = 0

        head_ok = instance.base_commit in (probe.get("testbed_head"), probe.get("testbed_parent"))
        status[instance.instance_id] = {
            "image": image,
            "status": "pulled",
            "digest": info.digest,
            "image_id": info.image_id,
            "python_version": probe.get("python"),
            "pytest_version": probe.get("pytest"),
            "testbed_head": probe.get("testbed_head"),
            "testbed_parent": probe.get("testbed_parent"),
            "testbed_matches_base": head_ok,
            "ignored_files": probe.get("ignored_files"),
            "pulled_at": _now_iso(),
        }
        _write_json(IMAGES_FILE, status)
        done += 1
        mark = "" if head_ok else "  ⚠ /testbed 不在 base_commit 上"
        print(
            f"    ✓ python {probe.get('python')}，pytest {probe.get('pytest')}，"
            f"构建产物 {probe.get('ignored_files')} 个，digest {str(info.digest)[:19]}…{mark}"
        )

    print(f"\n拉到 {done}，失败 {failed}，已有跳过 {skipped}；状态在 {IMAGES_FILE}")
    return 1 if failed else 0


# ══════════════════════════════════════════════════════════════
# fetch-blobs / load：Windows 侧下载，WSL 侧装载
# ══════════════════════════════════════════════════════════════

MANIFESTS_DIR = CACHE_DIR / "manifests"


def image_manifest(
    client: httpx.Client, image: str, *, registry: str = REGISTRY_URL
) -> dict[str, Any]:
    """读一个镜像 amd64 版的**完整** manifest（含 config 和 layers 的描述符），原样返回。

    `image_layers()` 只要层的大小；装载要把 manifest 原文和 config 一起塞进 OCI 布局，
    所以这里连 mediaType 一起保留。
    """
    repository, _, tag = image.partition(":")
    headers = _auth_headers(_registry_token(client, registry, repository), MANIFEST_ACCEPT)
    manifest = _get_json(client, f"{registry}/v2/{repository}/manifests/{tag}", headers=headers)
    if "manifests" in manifest:
        for entry in manifest["manifests"]:
            platform = entry.get("platform") or {}
            if platform.get("architecture") == "amd64" and platform.get("os") == "linux":
                manifest = _get_json(
                    client,
                    f"{registry}/v2/{repository}/manifests/{entry['digest']}",
                    headers=headers,
                )
                break
        else:
            raise SwebenchImportError(f"{image} 没有 linux/amd64 的 manifest")
    result: dict[str, Any] = manifest
    return result


def _blob_ok(digest: str, size: int) -> bool:
    """D 盘上这个层在不在、大小对不对。哈希留到 `load` 时校验（40 GB 全算一遍要几分钟）。"""
    path = WSL_BLOB_DIR / digest.removeprefix("sha256:")
    return path.is_file() and path.stat().st_size == size


def fetch_blob_via_windows(repository: str, digest: str, token: str, *, timeout_s: int) -> None:
    """用 Windows 的 curl.exe 把一个层下到 D 盘。断了续传（`-C -`），连不上重试。"""
    hex_digest = digest.removeprefix("sha256:")
    command = [
        "curl.exe",
        "-sS",
        "-L",
        "-x",
        WINDOWS_PROXY,
        "--retry",
        "12",
        "--retry-all-errors",
        "--retry-delay",
        "3",
        "-C",
        "-",
        # 30 秒里平均不到 50 KB/s 就当挂死掐断，让 --retry 带着 -C - 续传重来。
        # 只靠 --retry 不够：挂着不动不算失败，要等 -m 那 30 分钟才会放弃
        "--speed-limit",
        "51200",
        "--speed-time",
        "30",
        "-m",
        str(timeout_s),
        "-H",
        f"Authorization: Bearer {token}",
        f"{REGISTRY_URL}/v2/{repository}/blobs/{digest}",
        "-o",
        f"{WINDOWS_BLOB_DIR}\\{hex_digest}",
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode == 33:
        # Docker Hub 的层是 307 跳到 CDN 的，续传的 Range 有时在跳转后不被认（curl 33：
        # "does not seem to support byte ranges"）。半截文件删掉，这一层从头来一遍
        (WSL_BLOB_DIR / hex_digest).unlink(missing_ok=True)
        command = [arg for arg in command if arg not in ("-C", "-")]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise SwebenchImportError(
            f"curl.exe 退出码 {completed.returncode}：{completed.stderr.strip()[-200:]}"
        )


def cmd_fetch_blobs(args: argparse.Namespace) -> int:
    _, chosen = _sample_and_instances(args)
    if args.only:
        wanted = set(args.only)
        chosen = [instance for instance in chosen if instance.instance_id in wanted]
    WSL_BLOB_DIR.mkdir(parents=True, exist_ok=True)
    MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
    images = _read_json(IMAGES_FILE)
    fetched = skipped = failed = 0
    seen: set[str] = set()

    with make_client(timeout_s=60.0) as client:
        for instance in _pull_order(chosen)[: args.limit]:
            if (images.get(instance.instance_id) or {}).get(
                "status"
            ) == "pulled" and not args.force:
                continue  # 镜像已经在 docker 里了，不用再下
            image = official_image(instance.instance_id)
            repository, _, _tag = image.partition(":")
            try:
                manifest = image_manifest(client, image)
                token = _registry_token(client, REGISTRY_URL, repository)
            except (SwebenchImportError, httpx.HTTPError) as exc:
                failed += 1
                print(f"  ✗ {instance.instance_id:<36} manifest 拿不到：{str(exc)[:80]}")
                continue
            (MANIFESTS_DIR / f"{instance.instance_id}.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
            )
            blobs = [manifest["config"], *manifest["layers"]]
            todo = [
                b
                for b in blobs
                if b["digest"] not in seen and not _blob_ok(b["digest"], int(b["size"]))
            ]
            seen.update(b["digest"] for b in blobs)
            total_mb = sum(int(b["size"]) for b in todo) / 1e6
            print(f"  {instance.instance_id:<36} 要下 {len(todo)} 个层，{total_mb:,.0f} MB")
            started = time.monotonic()
            ok = True
            for blob in todo:
                digest, size = str(blob["digest"]), int(blob["size"])
                try:
                    fetch_blob_via_windows(repository, digest, token, timeout_s=args.timeout)
                except SwebenchImportError as exc:
                    ok = False
                    print(f"    ✗ {digest[7:19]} {size / 1e6:,.0f} MB：{exc}")
                    break
                if not _blob_ok(digest, size):
                    ok = False
                    print(f"    ✗ {digest[7:19]} 下完大小不对，删掉重来")
                    (WSL_BLOB_DIR / digest.removeprefix("sha256:")).unlink(missing_ok=True)
                    break
            if ok:
                fetched += 1
                seconds = time.monotonic() - started
                rate = total_mb / seconds if seconds > 0 and total_mb else 0.0
                print(f"    ✓ {seconds:.0f} 秒，{rate:,.1f} MB/s")
            else:
                failed += 1
    print(f"\n下好 {fetched}，失败 {failed}，跳过（docker 里已有）{skipped}")
    print(f"层在 {WINDOWS_BLOB_DIR}")
    return 1 if failed else 0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def oci_layout_tar(manifest: Mapping[str, Any], image: str, sink: IO[bytes]) -> None:
    """把 manifest + config + 层拼成 OCI 布局的 tar，写进 `sink`（`docker load` 的标准输入）。

    不落中间文件：一个镜像 1–3 GB，50 个镜像落一遍 tar 就是 100 多 GB 磁盘。
    `index.json` 里 `org.opencontainers.image.ref.name` 那条注解就是装载后的镜像名。
    """
    manifest_bytes = json.dumps(manifest, separators=(",", ":"), sort_keys=False).encode("utf-8")
    manifest_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": manifest.get(
                    "mediaType", "application/vnd.oci.image.manifest.v1+json"
                ),
                "digest": manifest_digest,
                "size": len(manifest_bytes),
                "annotations": {"org.opencontainers.image.ref.name": image},
            }
        ],
    }
    with tarfile.open(fileobj=sink, mode="w|") as tar:

        def add_bytes(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))

        add_bytes("oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
        add_bytes("index.json", json.dumps(index, separators=(",", ":")).encode("utf-8"))
        add_bytes(f"blobs/sha256/{manifest_digest[7:]}", manifest_bytes)
        for blob in (manifest["config"], *manifest["layers"]):
            hex_digest = str(blob["digest"]).removeprefix("sha256:")
            tar.add(
                WSL_BLOB_DIR / hex_digest, arcname=f"blobs/sha256/{hex_digest}", recursive=False
            )


def load_image_from_blobs(manifest: Mapping[str, Any], image: str) -> str:
    """校验每个层的哈希，然后 `docker load`。返回 docker 的输出。"""
    for blob in (manifest["config"], *manifest["layers"]):
        digest = str(blob["digest"])
        path = WSL_BLOB_DIR / digest.removeprefix("sha256:")
        if not path.is_file():
            raise SwebenchImportError(f"层 {digest[:19]} 还没下到 D 盘")
        actual = _sha256_file(path)
        if actual != digest.removeprefix("sha256:"):
            path.unlink()
            raise SwebenchImportError(f"层 {digest[:19]} 哈希对不上（已删，重跑 fetch-blobs）")
    loader = subprocess.Popen(
        ["docker", "load"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    assert loader.stdin is not None
    try:
        oci_layout_tar(manifest, image, loader.stdin)
    finally:
        loader.stdin.close()
    stdout, stderr = loader.communicate(timeout=3600)
    if loader.returncode != 0:
        raise SwebenchImportError(f"docker load 失败：{stderr.decode(errors='replace')[-300:]}")
    return stdout.decode(errors="replace").strip()


def cmd_load(args: argparse.Namespace) -> int:
    _, chosen = _sample_and_instances(args)
    if args.only:
        wanted = set(args.only)
        chosen = [instance for instance in chosen if instance.instance_id in wanted]
    status = _read_json(IMAGES_FILE)
    client = get_docker_client()
    loaded = failed = skipped = 0

    for instance in _pull_order(chosen)[: args.limit]:
        image = official_image(instance.instance_id)
        record = dict(status.get(instance.instance_id) or {})
        if record.get("status") == "pulled" and not args.force and _image_present(client, image):
            skipped += 1
            continue
        manifest_path = MANIFESTS_DIR / f"{instance.instance_id}.json"
        if not manifest_path.exists():
            print(f"  · {instance.instance_id:<36} 还没 fetch-blobs，跳过")
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        try:
            if not _image_present(client, image) or args.force:
                print(f"  {instance.instance_id:<36} 校验 + docker load …", end="", flush=True)
                load_image_from_blobs(manifest, image)
                print(" 完成")
            info = inspect_image(image, client=client)
            probe = probe_image(client, image)
        except Exception as exc:  # 校验、装载、探测任何一步失败都逐条报
            failed += 1
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            print(f"    ✗ {error}")
            status[instance.instance_id] = {
                **record,
                "image": image,
                "status": "failed",
                "error": error,
                "attempted_at": _now_iso(),
            }
            _write_json(IMAGES_FILE, status)
            continue
        head_ok = instance.base_commit in (probe.get("testbed_head"), probe.get("testbed_parent"))
        status[instance.instance_id] = {
            "image": image,
            "status": "pulled",
            "source": "windows-curl",
            "digest": info.digest,
            "image_id": info.image_id,
            "python_version": probe.get("python"),
            "pytest_version": probe.get("pytest"),
            "testbed_head": probe.get("testbed_head"),
            "testbed_parent": probe.get("testbed_parent"),
            "testbed_matches_base": head_ok,
            "ignored_files": probe.get("ignored_files"),
            "pulled_at": _now_iso(),
        }
        _write_json(IMAGES_FILE, status)
        loaded += 1
        mark = "" if head_ok else "  ⚠ /testbed 不在 base_commit 上"
        print(
            f"    ✓ python {probe.get('python')}，pytest {probe.get('pytest')}，"
            f"digest {str(info.digest)[:19]}…{mark}"
        )
    print(f"\n装载 {loaded}，失败 {failed}，已有跳过 {skipped}；状态在 {IMAGES_FILE}")
    return 1 if failed else 0


# ══════════════════════════════════════════════════════════════
# mirror
# ══════════════════════════════════════════════════════════════


def ensure_shallow_mirror(
    mirrors: MirrorManager, repo_name: str, repo_url: str, commits: Iterable[str], *, timeout_s: int
) -> dict[str, str]:
    """让 `var/mirrors/<owner>__<repo>.git` 里有这些 commit，**不 clone 全史**。

    `git clone --mirror` 拉 astropy / matplotlib 要几百 MB 还常被代理掐断；
    `git fetch --depth 1 origin <sha>` 只拿那个 commit 的树，几十 MB 一条。
    GitHub 允许按 SHA 拉（`uploadpack.allowReachableSHA1InWant`），
    E8-T3 给 xorbitsai 补 base_commit 时验证过这条路。
    物化只要 commit 对象和它的树在（`git archive <sha>`），浅的够用。
    """
    path = mirrors.path_for(repo_name)
    if not mirrors.exists(repo_name):
        path.mkdir(parents=True, exist_ok=True)
        run_git(["init", "--bare", "--quiet", str(path)], timeout_s=60)
    # origin 在就 set-url、不在就 add：上次建到一半的目录再来一遍 `remote add` 会报"已存在"
    if run_git(["remote", "get-url", "origin"], cwd=path, timeout_s=60, check=False).returncode:
        run_git(["remote", "add", "origin", "--", repo_url], cwd=path, timeout_s=60)
    else:
        run_git(["remote", "set-url", "origin", "--", repo_url], cwd=path, timeout_s=60)
    pin_archive_attributes(path)

    outcome: dict[str, str] = {}
    for commit in sorted(set(commits)):
        if mirrors.has_commit(repo_name, commit):
            outcome[commit] = "present"
            continue
        error = ""
        for attempt in range(1, 4):
            try:
                run_git(
                    ["fetch", "--depth", "1", "--quiet", "origin", commit],
                    cwd=path,
                    timeout_s=timeout_s,
                )
                break
            except GitError as exc:
                error = str(exc).splitlines()[-1][:160]
                print(f"    {commit[:12]} 第 {attempt} 次 fetch 失败：{error}")
                time.sleep(5)
        outcome[commit] = "fetched" if mirrors.has_commit(repo_name, commit) else f"failed: {error}"
    return outcome


def cmd_mirror(args: argparse.Namespace) -> int:
    _, chosen = _sample_and_instances(args)
    settings = get_settings()
    mirrors = MirrorManager(Path(settings.mirror_root), timeout_s=settings.git_timeout_s)
    state = _read_json(MIRRORS_FILE)
    failed = 0

    by_repo: dict[str, list[VerifiedInstance]] = {}
    for instance in chosen:
        by_repo.setdefault(instance.repo, []).append(instance)
    for repo, items in sorted(by_repo.items()):
        url = f"https://github.com/{repo}.git"
        print(f"  {repo}（{len(items)} 个 base_commit）")
        outcome = ensure_shallow_mirror(
            mirrors, repo, url, (i.base_commit for i in items), timeout_s=settings.git_timeout_s
        )
        for commit, result in outcome.items():
            if result.startswith("failed"):
                failed += 1
            print(f"    {commit[:12]} {result}")
        state[repo] = {**(state.get(repo) or {}), **outcome, "updated_at": _now_iso()}
        _write_json(MIRRORS_FILE, state)

    print(f"\n{'全部备好' if not failed else f'{failed} 个 commit 没拉到'}；状态在 {MIRRORS_FILE}")
    return 1 if failed else 0


# ══════════════════════════════════════════════════════════════
# import
# ══════════════════════════════════════════════════════════════


def _mark_environment_ready(
    session: Session, environment_id: str, record: Mapping[str, Any]
) -> None:
    """官方镜像已经在本地：把 digest 和 READY 写上，`cli.images build` 那一步这里用不着。

    `upsert_environment` 只在新建时写 `image_tag`，其余列它不碰，所以这里补。
    """
    session.execute(
        sa.update(EnvironmentSpec)
        .where(EnvironmentSpec.environment_id == environment_id)
        .values(
            image_tag=record.get("image"),
            image_digest=record.get("digest"),
            build_status=ImageBuildStatus.READY,
            built_at=datetime.fromisoformat(str(record["pulled_at"]))
            if record.get("pulled_at")
            else None,
        )
    )


def cmd_import(args: argparse.Namespace) -> int:
    _, chosen = _sample_and_instances(args)
    images = _read_json(IMAGES_FILE)
    factory = None if args.dry_run else create_session_factory(create_db_engine())

    created = updated = skipped = failed = 0
    fallback: list[str] = []
    for instance in chosen:
        record = images.get(instance.instance_id) or {}
        python_version = str(record.get("python_version") or "unknown")
        if record.get("status") == "pulled" and record.get("source") == "local-build":
            environment = built_environment(
                instance, python_version=python_version, image_tag=str(record["image"])
            )
        elif record.get("status") == "pulled":
            environment = official_environment(instance, python_version=python_version)
        elif args.allow_unpulled:
            environment = fallback_environment(instance)
            fallback.append(instance.instance_id)
        else:
            skipped += 1
            print(f"  · {instance.instance_id:<36} 镜像还没拉到，跳过（--allow-unpulled 退回自建）")
            continue

        try:
            task = build_task(instance, environment)
        except Exception as exc:  # 组装失败逐条报，不整批崩
            failed += 1
            print(f"  ✗ {instance.instance_id:<36} 组装失败：{str(exc).strip()[:120]}")
            continue

        if factory is None:
            created += 1
            counts = f"F2P {len(task.fail_to_pass):>2} P2P {len(task.pass_to_pass):>5}"
            print(f"  · {task.task_id:<36} {environment.kind:<9} {counts}")
            continue

        with session_scope(factory) as session:
            is_new = upsert_task(
                session,
                task,
                {environment.environment_id: environment.spec_row()},
                patch_uri_scheme="swebench",
            )
            if environment.kind in ("official", "built"):
                _mark_environment_ready(session, environment.environment_id, record)
        created += int(is_new)
        updated += int(not is_new)
        counts = f"F2P {len(task.fail_to_pass):>2} P2P {len(task.pass_to_pass):>5}"
        mark = "+" if is_new else "~"
        print(
            f"  {mark} {task.task_id:<36} {environment.kind:<9} {task.difficulty.value:<7} {counts}"
        )

    print(f"\n新建 {created}，更新 {updated}，跳过 {skipped}，失败 {failed}")
    if fallback:
        print("退回自建规格的（镜像没拉到，题目停在 DISCOVERED，等 images/envs/ 配方）：")
        print("  " + "、".join(fallback))
    if args.dry_run:
        print("（--dry-run：没有写库）")
    else:
        print(
            "\n下一步：\n"
            f"  uv run python -m cli.validate run --dataset {DATASET_ID} --scope declared"
        )
    return 1 if failed else 0


# ══════════════════════════════════════════════════════════════
# review-csv：给人工终审出对照表（§7.4 的 REVIEW_REQUIRED → 人工 → VALID / INVALID）
# ══════════════════════════════════════════════════════════════

#: `review_flags()` 里"题面太短"那一条的开头。官方题落到这一条时可以按既定政策收：
#: SWE-bench Verified 的 500 道题本身就是人工核过"题面够不够"才进 Verified 的，
#: 拿我们 200 字的规则去否决那次人工标注没有道理（2026-09-16 用户拍板）。
SHORT_ISSUE_FLAG_PREFIX = "issue_body 只有"
SHORT_ISSUE_ACCEPT_REASON = (
    "SWE-bench Verified 已人工核过题面；短是官方原文，按 2026-09-16 的既定政策收"
)

REVIEW_COLUMNS = (
    "task_id",
    "pr",
    "validation_state",
    "difficulty",
    "f2p_count",
    "p2p_count",
    "issue_title",
    "fail_to_pass",
    "review",
    "verdict",
    "reason",
)


def _review_reason(evidence_uri: str | None) -> str:
    """从验证证据里取 `review` 那一句（为什么停在 REVIEW_REQUIRED）。取不到就空着。"""
    if not evidence_uri:
        return ""
    try:
        store = create_artifact_store(get_settings())
        evidence = json.loads(store.get(key_from_uri(evidence_uri)).decode("utf-8"))
    except Exception:  # 证据读不出来不该让整张表出不来，这一格空着让人自己查
        return ""
    return str(evidence.get("review") or "")


def _review_rows(session: Session, *, accept_short_issue: bool) -> list[dict[str, Any]]:
    """停在 REVIEW_REQUIRED 的官方题，一行一道。

    只有"题面太短"这**一条**理由的，按既定政策直接填 ACCEPT；其余理由（F2P 超过 20 条、
    P2P 为空、基线逼近超时……）verdict 留空，**要人看**。
    """
    tasks = session.execute(
        sa.select(BenchmarkTask)
        .where(BenchmarkTask.raw_definition["dataset_id"].astext == DATASET_ID)
        .where(BenchmarkTask.validation_state == TaskValidationState.REVIEW_REQUIRED)
        .order_by(BenchmarkTask.task_id)
    ).scalars()
    rows = []
    for task in tasks:
        review = _review_reason(task.validation_evidence_uri)
        reasons = [part for part in review.split("；") if part]
        only_short = bool(reasons) and all(r.startswith(SHORT_ISSUE_FLAG_PREFIX) for r in reasons)
        verdict, reason = (
            ("ACCEPT", SHORT_ISSUE_ACCEPT_REASON) if only_short and accept_short_issue else ("", "")
        )
        rows.append(
            {
                "task_id": task.task_id,
                "pr": task.task_id.rsplit("-", 1)[-1],
                "validation_state": task.validation_state.value,
                "difficulty": task.difficulty.value,
                "f2p_count": len(task.fail_to_pass),
                "p2p_count": len(task.pass_to_pass),
                "issue_title": task.issue_title,
                "fail_to_pass": "\n".join(task.fail_to_pass),
                "review": review,
                "verdict": verdict,
                "reason": reason,
            }
        )
    return rows


def cmd_review_csv(args: argparse.Namespace) -> int:
    with session_scope(create_session_factory(create_db_engine())) as session:
        rows = _review_rows(session, accept_short_issue=not args.no_auto_accept)
    if not rows:
        print("没有停在 REVIEW_REQUIRED 的官方题")
        return 0
    out = (
        Path(args.out)
        if args.out
        else SAMPLE_DIR / f"review-{datetime.now():%Y-%m-%d}-official.csv"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    filled = sum(1 for row in rows if row["verdict"])
    print(
        f"{len(rows)} 道停在 REVIEW_REQUIRED，其中 {filled} 道只因题面太短、已按政策填 ACCEPT，"
        f"{len(rows) - filled} 道要人填"
    )
    for row in rows:
        mark = row["verdict"] or "（待填）"
        print(f"  {row['task_id']:<36} {mark:<8} {row['review'][:70]}")
    print(f"\n写入 {out}\n导回：cd backend && uv run python -m cli.promote import-review {out}")
    return 0


# ══════════════════════════════════════════════════════════════
# report
# ══════════════════════════════════════════════════════════════


def _validation_states(session: Session) -> dict[str, tuple[str, str | None]]:
    rows = session.execute(
        sa.select(
            BenchmarkTask.task_id, BenchmarkTask.validation_state, BenchmarkTask.invalid_reason_code
        ).where(BenchmarkTask.raw_definition["dataset_id"].astext == DATASET_ID)
    ).all()
    return {task_id: (state.value, reason) for task_id, state, reason in rows}


def build_funnel(
    instances: Sequence[VerifiedInstance],
    screened: Sequence[Screened],
    sample: Mapping[str, Any] | None,
    images: Mapping[str, Any],
    mirrors: Mapping[str, Any],
    states: Mapping[str, tuple[str, str | None]],
) -> Funnel:
    """把各处的状态凑成一张漏斗。纯函数，测试里直接喂字典。"""
    funnel = Funnel(official_total=len(instances))
    funnel.offline = Counter(item.bucket for item in screened)
    by_id = {instance.instance_id: instance for instance in instances}
    for item in screened:
        if item.ok:
            funnel.per_repo.setdefault(item.instance.repo, {"pool": 0, "sampled": 0, "valid": 0})
            funnel.per_repo[item.instance.repo]["pool"] += 1

    chosen = list(sample["chosen"]) if sample else []
    funnel.sampled = len(chosen)
    for instance_id in chosen:
        repo = by_id[instance_id].repo if instance_id in by_id else "?"
        funnel.per_repo.setdefault(repo, {"pool": 0, "sampled": 0, "valid": 0})["sampled"] += 1
        record = images.get(instance_id) or {}
        if record.get("status") == "pulled":
            funnel.image_pulled += 1
        else:
            funnel.image_unavailable.append(instance_id)
        instance = by_id.get(instance_id)
        repo_state = mirrors.get(instance.repo, {}) if instance else {}
        result = str(repo_state.get(instance.base_commit, "")) if instance else ""
        if result in ("present", "fetched"):
            funnel.mirror_ready += 1
        else:
            funnel.mirror_failed.append(instance_id)
        if instance_id in states:
            funnel.imported += 1
            state, reason = states[instance_id]
            label = f"{state}({reason})" if reason else state
            funnel.validation[label] += 1
            if state == "VALID":
                funnel.per_repo[repo]["valid"] += 1
    return funnel


def cmd_report(args: argparse.Namespace) -> int:
    instances = _load_all()
    screened = screen_all(instances)
    path = sample_path(args.seed, args.n)
    sample = load_sample(path) if path.exists() else None
    states: dict[str, tuple[str, str | None]] = {}
    if not args.no_db:
        with session_scope(create_session_factory(create_db_engine())) as session:
            states = _validation_states(session)
    funnel = build_funnel(
        instances, screened, sample, _read_json(IMAGES_FILE), _read_json(MIRRORS_FILE), states
    )
    text = render_funnel(funnel)
    print(text)
    if args.save:
        out = SAMPLE_DIR / f"import-report-{datetime.now():%Y-%m-%d}.md"
        out.write_text(
            f"# SWE-bench Verified 导入漏斗（{datetime.now():%Y-%m-%d}）\n\n{text}\n",
            encoding="utf-8",
        )
        print(f"\n已写入 {out}")
    return 0


# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════


def _add_sample_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help=f"抽样种子，默认 {DEFAULT_SEED}"
    )
    parser.add_argument(
        "--n", type=int, default=DEFAULT_SAMPLE_SIZE, help=f"抽几道，默认 {DEFAULT_SAMPLE_SIZE}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cli.swebench", description="SWE-bench Verified 官方题导入（E1-T7，§8.6）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="拉官方数据集到 var/cache/swebench/")
    p_fetch.add_argument("--refresh", action="store_true", help="已有也重拉")
    p_fetch.add_argument("--page-size", type=int, default=HF_PAGE_SIZE, help="每页几行（最多 100）")
    p_fetch.add_argument("--timeout", type=float, default=180.0, help="单个请求的超时（秒）")
    p_fetch.set_defaults(func=cmd_fetch)

    p_screen = sub.add_parser("screen", help="离线筛，打印漏斗第一层")
    p_screen.add_argument("-v", "--verbose", action="store_true", help="逐条列出被筛掉的")
    p_screen.set_defaults(func=cmd_screen)

    p_sample = sub.add_parser("sample", help="固定种子分层抽样，名单写进 datasets/swebench/")
    _add_sample_args(p_sample)
    p_sample.add_argument(
        "--check", action="store_true", help="不写文件，只核对已有名单和重算结果是否一致"
    )
    p_sample.set_defaults(func=cmd_sample)

    p_estimate = sub.add_parser("estimate", help="只读 manifest，算要下多少 GB")
    _add_sample_args(p_estimate)
    p_estimate.add_argument("--timeout", type=float, default=60.0)
    p_estimate.set_defaults(func=cmd_estimate)

    p_warm = sub.add_parser("warm", help="预热镜像站：每一层碰几秒，让它先去 Docker Hub 缓存")
    _add_sample_args(p_warm)
    p_warm.add_argument(
        "--mirror", default=DEFAULT_MIRROR_URL, help=f"镜像站，默认 {DEFAULT_MIRROR_URL}"
    )
    p_warm.add_argument("--only", action="append", help="只预热这几道（instance_id，可重复给）")
    p_warm.add_argument("--limit", type=int, default=None, help="最多预热几个镜像")
    p_warm.add_argument("--seconds", type=float, default=WARM_SECONDS_PER_LAYER, help="每层碰几秒")
    p_warm.add_argument("--timeout", type=float, default=20.0, help="单个请求的连接 / 读超时（秒）")
    p_warm.set_defaults(func=cmd_warm)

    p_pull = sub.add_parser("pull", help="拉官方镜像并探测")
    _add_sample_args(p_pull)
    p_pull.add_argument("--only", action="append", help="只拉这几道（instance_id，可重复给）")
    p_pull.add_argument("--limit", type=int, default=None, help="最多拉几个")
    p_pull.add_argument("--force", action="store_true", help="本地已有也重拉重探")
    p_pull.add_argument(
        "--give-up-after",
        type=int,
        default=3,
        help="连续几个失败就停下这一轮（代理挂了时别一个个白等），默认 3",
    )
    p_pull.add_argument("--dry-run", action="store_true", help="只列不拉")
    p_pull.set_defaults(func=cmd_pull)

    p_fetch_blobs = sub.add_parser("fetch-blobs", help="用 Windows 的 curl.exe 把镜像的层下到 D 盘")
    _add_sample_args(p_fetch_blobs)
    p_fetch_blobs.add_argument(
        "--only", action="append", help="只下这几道（instance_id，可重复给）"
    )
    p_fetch_blobs.add_argument("--limit", type=int, default=None, help="最多下几个镜像")
    p_fetch_blobs.add_argument("--force", action="store_true", help="docker 里已有的也重新下")
    p_fetch_blobs.add_argument(
        "--timeout", type=int, default=BLOB_TIMEOUT_S, help="单个层最多等几秒"
    )
    p_fetch_blobs.set_defaults(func=cmd_fetch_blobs)

    p_load = sub.add_parser("load", help="把 D 盘上的层拼成 OCI 布局 docker load 进来，再探测登记")
    _add_sample_args(p_load)
    p_load.add_argument("--only", action="append", help="只装这几道（instance_id，可重复给）")
    p_load.add_argument("--limit", type=int, default=None, help="最多装几个镜像")
    p_load.add_argument("--force", action="store_true", help="docker 里已有的也重装重探")
    p_load.set_defaults(func=cmd_load)

    from cli.swebench_build import add_build_parser

    add_build_parser(sub, _add_sample_args)

    p_mirror = sub.add_parser("mirror", help="备好 git 镜像（浅拉 base_commit）")
    _add_sample_args(p_mirror)
    p_mirror.set_defaults(func=cmd_mirror)

    p_import = sub.add_parser("import", help="组装题目和环境规格入库")
    _add_sample_args(p_import)
    p_import.add_argument(
        "--allow-unpulled", action="store_true", help="镜像没拉到的也入库，退回按桶自建的环境规格"
    )
    p_import.add_argument("--dry-run", action="store_true", help="只组装不写库")
    p_import.set_defaults(func=cmd_import)

    p_review = sub.add_parser("review-csv", help="停在 REVIEW_REQUIRED 的官方题 → 终审对照表")
    p_review.add_argument(
        "--out", help="输出路径，默认 datasets/swebench/review-<日期>-official.csv"
    )
    p_review.add_argument(
        "--no-auto-accept", action="store_true", help="题面太短的也不自动填 ACCEPT，全部留给人填"
    )
    p_review.set_defaults(func=cmd_review_csv)

    p_report = sub.add_parser("report", help="导入漏斗")
    _add_sample_args(p_report)
    p_report.add_argument("--no-db", action="store_true", help="不连库（只看离线几层）")
    p_report.add_argument(
        "--save", action="store_true", help="写进 datasets/swebench/import-report-<日期>.md"
    )
    p_report.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result: int = args.func(args)
    except SwebenchImportError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_funnel",
    "build_parser",
    "ensure_shallow_mirror",
    "fetch_rows",
    "image_layers",
    "main",
    "pull_image",
    "sample_path",
]
