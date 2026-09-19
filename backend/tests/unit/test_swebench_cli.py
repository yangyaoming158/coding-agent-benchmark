"""`cli.swebench` 里不联网、不起容器、不连库就能验的部分（E1-T7）。

HTTP 用 `httpx.MockTransport` 顶替：`fetch` 的分页、截断检测、重试都在这一层。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.benchmark.swebench_import import (
    BUCKET_OK,
    SwebenchImportError,
    parse_instance,
    screen_all,
)
from cli import swebench
from cli.swebench import build_funnel, build_parser, fetch_rows, image_layers, write_rows
from tests.unit.test_swebench_import import row

# ══════════════════════════════════════════════════════════════
# 参数
# ══════════════════════════════════════════════════════════════


def test_parser_has_every_step_with_defaults() -> None:
    parser = build_parser()
    for command in ("fetch", "screen", "sample", "estimate", "pull", "mirror", "import", "report"):
        args = parser.parse_args([command])
        assert args.command == command
    args = parser.parse_args(["sample"])
    expected = (swebench.DEFAULT_SEED, swebench.DEFAULT_SAMPLE_SIZE, False)
    assert (args.seed, args.n, args.check) == expected
    # 2026-09-16 晚 50 → 75，2026-09-18 75 → 100，前面的名单不变
    assert swebench.DEFAULT_SAMPLE_SIZE == 100
    args = parser.parse_args(["pull", "--only", "a__b-1", "--limit", "3", "--dry-run"])
    assert args.only == ["a__b-1"] and args.limit == 3 and args.dry_run
    args = parser.parse_args(["import", "--allow-unpulled", "--dry-run"])
    assert args.allow_unpulled and args.dry_run


def test_sample_path_encodes_seed_and_size() -> None:
    assert swebench.sample_path(20260915, 50).name == "sample-seed20260915-n50.json"


# ══════════════════════════════════════════════════════════════
# fetch
# ══════════════════════════════════════════════════════════════


def _rows_transport(
    rows: list[dict[str, Any]], *, truncate_at: int | None = None
) -> httpx.MockTransport:
    """假的 datasets-server：按 offset / length 分页；`truncate_at` 指定第几行标成截断。"""

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        offset, length = int(params["offset"]), int(params["length"])
        page = [
            {
                "row_idx": index,
                "row": rows[index],
                "truncated_cells": ["patch"] if index == truncate_at else [],
            }
            for index in range(offset, min(offset + length, len(rows)))
        ]
        return httpx.Response(
            200, json={"rows": page, "num_rows_total": len(rows), "num_rows_per_page": length}
        )

    return httpx.MockTransport(handler)


def test_fetch_rows_pages_through_the_whole_split() -> None:
    rows = [row(instance_id=f"pallets__flask-{index}") for index in range(7)]
    with httpx.Client(transport=_rows_transport(rows)) as client:
        fetched = fetch_rows(client, page_size=3)
    assert [item["instance_id"] for item in fetched] == [r["instance_id"] for r in rows]


def test_fetch_rows_refuses_truncated_cells() -> None:
    rows = [row(instance_id=f"pallets__flask-{index}") for index in range(3)]
    with (
        httpx.Client(transport=_rows_transport(rows, truncate_at=1)) as client,
        pytest.raises(SwebenchImportError, match="截断"),
    ):
        fetch_rows(client, page_size=2)


def test_fetch_rows_retries_transient_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(swebench.time, "sleep", lambda _s: None)
    calls = {"n": 0}
    rows = [row()]

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="try later")
        page = [{"row_idx": 0, "row": rows[0], "truncated_cells": []}]
        return httpx.Response(200, json={"rows": page, "num_rows_total": 1, "num_rows_per_page": 1})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetched = fetch_rows(client, page_size=50)
    assert len(fetched) == 1 and calls["n"] == 2


def test_write_rows_records_fingerprint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(swebench, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(swebench, "ROWS_FILE", tmp_path / "verified.jsonl")
    monkeypatch.setattr(swebench, "META_FILE", tmp_path / "verified.meta.json")
    meta = write_rows([row()], revision="abc123")
    assert meta["num_rows"] == 1 and meta["hf_revision"] == "abc123" and len(meta["sha256"]) == 64
    lines = (tmp_path / "verified.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["instance_id"] == "pallets__flask-4045"
    assert json.loads((tmp_path / "verified.meta.json").read_text())["sha256"] == meta["sha256"]


# ══════════════════════════════════════════════════════════════
# estimate（只读 manifest）
# ══════════════════════════════════════════════════════════════


def test_image_layers_follows_multi_arch_index() -> None:
    """令牌按 registry 协议发现：先无凭证请求 → 401 的 WWW-Authenticate 里读 realm 和 service。"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://auth.example/token"):
            assert request.url.params["service"] == "mirror.example"
            assert request.url.params["scope"].startswith("repository:swebench/")
            return httpx.Response(200, json={"access_token": "t"})
        if url.endswith("/manifests/latest") and "Authorization" not in request.headers:
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer realm="https://auth.example/token",'
                    'service="mirror.example"'
                },
            )
        if url.endswith("/manifests/latest"):
            assert request.headers["Authorization"] == "Bearer t"
            return httpx.Response(
                200,
                json={
                    "manifests": [
                        {
                            "digest": "sha256:arm",
                            "platform": {"architecture": "arm64", "os": "linux"},
                        },
                        {
                            "digest": "sha256:amd",
                            "platform": {"architecture": "amd64", "os": "linux"},
                        },
                    ]
                },
            )
        if url.endswith("/manifests/sha256:amd"):
            return httpx.Response(
                200,
                json={
                    "layers": [
                        {"digest": "sha256:l1", "size": 10},
                        {"digest": "sha256:l2", "size": 20},
                    ]
                },
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        layers = image_layers(
            client,
            "swebench/sweb.eval.x86_64.pallets_1776_flask-4045:latest",
            registry="https://mirror.example",
        )
    assert layers == [("sha256:l1", 10), ("sha256:l2", 20)]


def test_touch_blob_counts_bytes_and_stops_at_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """预热只是"碰一下"：到点就断开，返回这段时间收到的字节数；连不上按 0 算。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer t"
        return httpx.Response(200, content=b"x" * (3 * 256 * 1024))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        got = swebench.touch_blob(client, "https://m", "swebench/x", "sha256:l1", "t", seconds=5)
    assert got == 3 * 256 * 1024

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with httpx.Client(transport=httpx.MockTransport(broken)) as client:
        assert (
            swebench.touch_blob(client, "https://m", "swebench/x", "sha256:l1", "t", seconds=5) == 0
        )


# ══════════════════════════════════════════════════════════════
# report：漏斗从各处状态拼出来
# ══════════════════════════════════════════════════════════════


def test_build_funnel_counts_each_layer() -> None:
    instances = [
        parse_instance(row(instance_id="pallets__flask-1")),
        parse_instance(row(instance_id="pallets__flask-2")),
        parse_instance(row(repo="django/django", instance_id="django__django-3")),
    ]
    screened = screen_all(instances)
    sample = {"chosen": ["pallets__flask-1", "pallets__flask-2"]}
    images = {"pallets__flask-1": {"status": "pulled"}, "pallets__flask-2": {"status": "failed"}}
    base = instances[0].base_commit
    mirrors = {"pallets/flask": {base: "fetched"}}
    states = {"pallets__flask-1": ("VALID", None)}

    funnel = build_funnel(instances, screened, sample, images, mirrors, states)

    assert funnel.official_total == 3
    assert funnel.offline[BUCKET_OK] == 2
    assert funnel.sampled == 2
    assert funnel.image_pulled == 1 and funnel.image_unavailable == ["pallets__flask-2"]
    assert funnel.mirror_ready == 2 and funnel.mirror_failed == []
    assert funnel.imported == 1 and funnel.validation == {"VALID": 1}
    assert funnel.per_repo["pallets/flask"] == {"pool": 2, "sampled": 2, "valid": 1}


# ══════════════════════════════════════════════════════════════
# pull：卡死判定
# ══════════════════════════════════════════════════════════════


class _FakeResponse:
    closed = False

    def close(self) -> None:
        self.closed = True


class _FakeApi:
    """只实现 `pull_image` 用到的三个底层方法，事件按脚本回放。"""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.response = _FakeResponse()
        self.kwargs: dict[str, Any] = {}

    def _url(self, path: str) -> str:
        return f"http+docker://localhost{path}"

    def _post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.kwargs = kwargs
        return self.response

    def _stream_helper(self, response: _FakeResponse, decode: bool = False) -> Any:
        yield from self.events


class _FakeClient:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.api = _FakeApi(events)


def test_pull_image_sets_a_read_timeout_and_finishes_on_progress() -> None:
    events = [
        {"status": "Pulling from x", "id": "latest"},
        {"status": "Downloading", "id": "aa", "progressDetail": {"current": 10, "total": 20}},
        {"status": "Downloading", "id": "aa", "progressDetail": {"current": 20, "total": 20}},
        {"status": "Pull complete", "id": "aa"},
    ]
    client = _FakeClient(events)
    swebench.pull_image(client, "swebench/x:latest", stall_s=300)
    assert client.api.kwargs["timeout"] == 300
    assert client.api.kwargs["params"] == {"fromImage": "swebench/x", "tag": "latest"}
    assert not client.api.response.closed


def test_pull_image_raises_on_error_event() -> None:
    client = _FakeClient([{"error": "manifest unknown"}])
    with pytest.raises(SwebenchImportError, match="manifest unknown"):
        swebench.pull_image(client, "swebench/x:latest")


def test_pull_image_detects_a_trickle_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    """事件一直来、字节数不涨 —— 读超时抓不到这种，得看下载量。"""
    clock = {"now": 0.0}
    monkeypatch.setattr(swebench.time, "monotonic", lambda: clock["now"])

    def events() -> Any:
        yield {"status": "Downloading", "id": "aa", "progressDetail": {"current": 5, "total": 9}}
        for _ in range(5):
            clock["now"] += 100  # 每个事件隔 100 秒，字节数原地踏步
            yield {"status": "Downloading", "id": "aa", "progressDetail": {"current": 5}}

    client = _FakeClient([])
    client.api.events = events()  # type: ignore[assignment]
    with pytest.raises(SwebenchImportError, match="没有增长"):
        swebench.pull_image(client, "swebench/x:latest", stall_s=250)
    assert client.api.response.closed
