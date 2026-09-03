"""ArtifactStore 的契约测试（E0-T4 的验收标准）。

**这一套是按接口写的，不是按实现写的。** ADR-005 说制品存储要能靠一个配置项
在 local 和 minio 之间切换、业务代码零改动 —— 这句话只有在"两个实现跑同一套测试
都能过"的时候才成立。E10-T2 接 MinIO 时，往 `STORE_BACKENDS` 里加一行就行，
下面的用例一条都不用改。

文件末尾另有一组只针对 `LocalArtifactStore` 的测试（落盘位置、临时文件清理），
那些是实现细节，不属于契约。
"""

from __future__ import annotations

import gzip
import hashlib
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

import pytest

from app.domain.enums import ArtifactBackend
from app.storage import (
    ArtifactNotFoundError,
    ArtifactStore,
    InvalidArtifactKeyError,
    LocalArtifactStore,
)

#: 参与契约测试的后端。加 MinIO 时在这里加一个 id，并在 `store` 夹具里补一个分支
#: （连不上就 `pytest.skip`，不要让没起 MinIO 的人被红叉挡住）。
STORE_BACKENDS = ["local"]

TEXT = "第一行\nsecond line\n" * 200
DATA = TEXT.encode("utf-8")
KEY = "runs/1/task-runs/2/agent_stdout.log"


@pytest.fixture(params=STORE_BACKENDS)
def store(request: pytest.FixtureRequest, tmp_path: Path) -> ArtifactStore:
    if request.param == "local":
        return LocalArtifactStore(tmp_path / "artifacts")
    raise AssertionError(f"没有这个后端的夹具分支：{request.param}")


# ── 存取往返 ────────────────────────────────────────────────


@pytest.mark.parametrize("compress", [True, False])
def test_put_then_get_roundtrip(store: ArtifactStore, compress: bool) -> None:
    """存进去什么，读出来就是什么 —— 压缩与否对调用方不可见。"""
    store.put(KEY, DATA, content_type="text/plain", compress=compress)
    assert store.get(KEY) == DATA


def test_get_binary_content(store: ArtifactStore) -> None:
    """二进制制品不能被当成文本处理坏了（补丁文件里可能有非 UTF-8 字节）。"""
    blob = bytes(range(256)) * 40
    store.put("tasks/t1/gold_patch.diff", blob, content_type="application/octet-stream")
    assert store.get("tasks/t1/gold_patch.diff") == blob


def test_put_from_file_object(store: ArtifactStore, tmp_path: Path) -> None:
    """入参可以是打开的文件对象。几 MB 的日志不该先整个读进内存。"""
    source = tmp_path / "big.log"
    payload = b"x" * (3 * 1024 * 1024 + 7)  # 跨过 1 MiB 的分块边界，且不是整数倍
    source.write_bytes(payload)
    with source.open("rb") as handle:
        ref = store.put(KEY, handle, content_type="text/plain")
    assert ref.size_bytes == len(payload)
    assert store.get(KEY) == payload


def test_open_streams_decompressed(store: ArtifactStore) -> None:
    """`open()` 拿到的流读出来也是原始内容。"""
    store.put(KEY, DATA, content_type="text/plain")
    with store.open(KEY) as stream:
        assert stream.read() == DATA


# ── ArtifactRef 的字段语义 ──────────────────────────────────


def test_ref_hash_is_of_original_content(store: ArtifactStore) -> None:
    """sha256 算的是**原始内容**，压不压都一样。

    这条很重要：哈希是内容的身份证，用来去重和校验完整性。要是按压缩后的字节算，
    同一份日志换个压缩级别哈希就变了，两条一模一样的日志会被当成两份不同的东西。
    """
    expected = hashlib.sha256(DATA).hexdigest()
    gz_ref = store.put("a/x.log", DATA, content_type="text/plain", compress=True)
    raw_ref = store.put("a/y.log", DATA, content_type="text/plain", compress=False)
    assert gz_ref.sha256 == raw_ref.sha256 == expected


def test_ref_size_is_original_stored_is_on_disk(store: ArtifactStore) -> None:
    """size_bytes 是原始大小，stored_bytes 是实际占的空间。

    日志这种重复度高的文本，压缩比常在 10:1（§17.2），所以这里能明确断言变小了。
    """
    ref = store.put(KEY, DATA, content_type="text/plain", compress=True)
    assert ref.size_bytes == len(DATA)
    assert ref.compressed is True
    assert ref.stored_bytes < ref.size_bytes


def test_ref_uncompressed_sizes_match(store: ArtifactStore) -> None:
    ref = store.put(KEY, DATA, content_type="text/plain", compress=False)
    assert ref.size_bytes == ref.stored_bytes == len(DATA)
    assert ref.compressed is False


def test_ref_uri_excludes_storage_root(store: ArtifactStore, tmp_path: Path) -> None:
    """uri 里不能出现根目录。

    数据库里存的是 uri。要是把 `/home/xxx/var/artifacts/...` 写进去，
    换台机器、改个挂载点、或者从 local 切到 minio，历史行就全指错地方了。
    """
    ref = store.put(KEY, DATA, content_type="text/plain")
    assert str(tmp_path) not in ref.uri
    assert KEY in ref.uri


def test_ref_carries_backend_and_content_type(store: ArtifactStore) -> None:
    """ref 的字段要能直接填进 artifacts 表。"""
    ref = store.put(KEY, DATA, content_type="text/plain; charset=utf-8")
    assert ref.key == KEY
    assert ref.content_type == "text/plain; charset=utf-8"
    assert ref.backend in set(ArtifactBackend)
    assert len(ref.sha256) == 64


# ── 覆盖、存在性、删除 ──────────────────────────────────────


def test_put_same_key_overwrites(store: ArtifactStore) -> None:
    store.put(KEY, b"first", content_type="text/plain")
    store.put(KEY, b"second", content_type="text/plain")
    assert store.get(KEY) == b"second"


def test_overwrite_with_different_compression(store: ArtifactStore) -> None:
    """同一个 key 换了压缩方式再存，读出来必须是新内容。

    这里藏着一个很难查的 bug：压缩版和非压缩版在磁盘上是两个不同的文件名
    （`x.log.gz` 和 `x.log`）。写新的时候不把旧的那份删掉，读的时候先找到过期的
    `.gz`，就会静默返回上一次的内容 —— 不报错，只是数据不对。
    """
    store.put(KEY, b"compressed version", content_type="text/plain", compress=True)
    store.put(KEY, b"plain version", content_type="text/plain", compress=False)
    assert store.get(KEY) == b"plain version"

    store.put(KEY, b"compressed again", content_type="text/plain", compress=True)
    assert store.get(KEY) == b"compressed again"


def test_exists(store: ArtifactStore) -> None:
    assert store.exists(KEY) is False
    store.put(KEY, DATA, content_type="text/plain")
    assert store.exists(KEY) is True


def test_delete(store: ArtifactStore) -> None:
    store.put(KEY, DATA, content_type="text/plain")
    store.delete(KEY)
    assert store.exists(KEY) is False


def test_delete_missing_is_noop(store: ArtifactStore) -> None:
    """删一个不存在的 key 不报错：重试删除时不用先查一次。"""
    store.delete("runs/9/task-runs/9/nothing.log")


# ── 读不存在的东西 ──────────────────────────────────────────


def test_get_missing_raises(store: ArtifactStore) -> None:
    with pytest.raises(ArtifactNotFoundError):
        store.get("runs/1/task-runs/404/agent_stdout.log")


def test_open_missing_raises(store: ArtifactStore) -> None:
    with pytest.raises(ArtifactNotFoundError):
        store.open("runs/1/task-runs/404/agent_stdout.log")


# ── url() ──────────────────────────────────────────────────


def test_url_returns_str_or_none(store: ArtifactStore) -> None:
    """契约只保证返回 `str | None`：MinIO 给签名 URL，Local 给 None。

    API 靠这个区别决定是 302 让浏览器直连，还是自己流式转发。
    """
    store.put(KEY, DATA, content_type="text/plain")
    result = store.url(KEY, expires_s=60)
    assert result is None or isinstance(result, str)


# ── key 校验 ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        ("", "空 key"),
        ("/runs/1/x.log", "绝对路径"),
        ("../../etc/passwd", "路径穿越"),
        ("runs/../../etc/passwd", "路径中间穿越"),
        ("runs//x.log", "空路径段"),
        ("runs/./x.log", "单点段"),
        ("runs/1/x.log.gz", "自带 .gz 后缀"),
        ("runs/1/日志.log", "非 ASCII 字符"),
        ("runs/1/x y.log", "空格"),
        ("runs\\1\\x.log", "反斜杠"),
        ("a" * 401, "超长"),
    ],
)
def test_invalid_keys_rejected(store: ArtifactStore, key: str, reason: str) -> None:
    """非法 key 一律拒绝，四个入口都拒。

    key 是拼出来的，题目 ID 和仓库名来自 GitHub，属于外部数据。一个 `../../`
    就能让制品写到根目录外面去 —— 这不是理论风险，是这条数据链路的现实。
    """
    with pytest.raises(InvalidArtifactKeyError):
        store.put(key, DATA, content_type="text/plain")
    with pytest.raises(InvalidArtifactKeyError):
        store.get(key)
    with pytest.raises(InvalidArtifactKeyError):
        store.exists(key)
    with pytest.raises(InvalidArtifactKeyError):
        store.delete(key)


def test_valid_keys_accepted(store: ArtifactStore) -> None:
    """§17.2 里那几种 key 都得能用（去掉 .gz 后缀，压缩由 put 决定）。"""
    for key in [
        "tasks/astropy__astropy-12907/gold_patch.diff",
        "tasks/t1/validation/2026-09-03T10-00-00/evidence.json",
        "envs/py311-numpy/build.log",
        "runs/12/task-runs/340/trajectory.jsonl",
        "runs/12/report.html",
    ]:
        store.put(key, b"ok", content_type="text/plain")
        assert store.get(key) == b"ok"


# ══════════════════════════════════════════════════════════
# 下面这些只针对 LocalArtifactStore，不属于契约
# ══════════════════════════════════════════════════════════


@pytest.fixture
def local_store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "artifacts")


def test_local_disk_layout_matches_spec(local_store: LocalArtifactStore) -> None:
    """落盘路径就是 §17.2 那张表的样子：压缩过的带 `.gz`，没压的不带。"""
    local_store.put(KEY, DATA, content_type="text/plain", compress=True)
    assert (local_store.root / "runs/1/task-runs/2/agent_stdout.log.gz").is_file()

    local_store.put("runs/1/report.html", b"<html>", content_type="text/html", compress=False)
    assert (local_store.root / "runs/1/report.html").is_file()


def test_local_file_is_real_gzip(local_store: LocalArtifactStore) -> None:
    """压缩的那份在磁盘上真的是 gzip，别的工具（zcat、浏览器）也能直接读。"""
    local_store.put(KEY, DATA, content_type="text/plain", compress=True)
    path = local_store.root / "runs/1/task-runs/2/agent_stdout.log.gz"
    assert gzip.decompress(path.read_bytes()) == DATA


def test_local_gzip_is_deterministic(local_store: LocalArtifactStore, tmp_path: Path) -> None:
    """同样的内容压两次，磁盘上的字节要完全一样。

    gzip 默认把当前时间写进文件头，不固定的话同一份内容每次存都是不同的文件，
    去重和"结果可复现"都没法谈。实现里用 `mtime=0` 关掉了这个行为。
    """
    other = LocalArtifactStore(tmp_path / "artifacts2")
    local_store.put(KEY, DATA, content_type="text/plain")
    other.put(KEY, DATA, content_type="text/plain")
    rel = "runs/1/task-runs/2/agent_stdout.log.gz"
    assert (local_store.root / rel).read_bytes() == (other.root / rel).read_bytes()


def test_local_leaves_no_temp_files(local_store: LocalArtifactStore) -> None:
    """写完不留临时文件。"""
    local_store.put(KEY, DATA, content_type="text/plain")
    leftovers = [p.name for p in local_store.root.rglob("*.tmp")]
    assert leftovers == []


def test_local_failed_put_leaves_nothing(local_store: LocalArtifactStore) -> None:
    """写到一半炸了，既不留临时文件，也不留半截的目标文件。

    这不是假想情况：评测容器被 OOM 杀掉是这个项目的日常（协议 C-06）。
    直接往目标文件写的话，会留下一个长度不对但看着正常的日志，读出来是半截内容，
    而且不报错。实现里先写临时文件再 `os.replace()`，就是为了挡这个。
    """

    class ExplodingStream(BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise OSError("磁盘炸了")

    with pytest.raises(OSError, match="磁盘炸了"):
        local_store.put(KEY, ExplodingStream(DATA), content_type="text/plain")

    assert list(local_store.root.rglob("*.tmp")) == []
    assert local_store.exists(KEY) is False


def test_local_rejects_symlink_escape(local_store: LocalArtifactStore, tmp_path: Path) -> None:
    """制品目录里有指向外面的软链时，不许顺着它写出去。

    `validate_key()` 只看字符串，挡不住软链。这条是第二道防线。
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (local_store.root / "escaped").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="跑出了制品根目录"):
        local_store.put("escaped/leak.log", DATA, content_type="text/plain")


def test_local_creates_root_eagerly(tmp_path: Path) -> None:
    """构造时就把根目录建出来 —— 目录没权限写要在启动时暴露，不能等评测跑到一半。"""
    root = tmp_path / "deep" / "nested" / "artifacts"
    assert LocalArtifactStore(root).root.is_dir()


def test_local_backend_is_local(local_store: LocalArtifactStore) -> None:
    assert local_store.backend is ArtifactBackend.LOCAL


def _rglob_files(root: Path) -> Iterator[Path]:
    return (p for p in root.rglob("*") if p.is_file())


def test_local_overwrite_keeps_exactly_one_file(local_store: LocalArtifactStore) -> None:
    """换压缩方式覆盖之后，磁盘上只剩一个文件，不留孤儿。"""
    local_store.put(KEY, b"a", content_type="text/plain", compress=True)
    local_store.put(KEY, b"b", content_type="text/plain", compress=False)
    assert len(list(_rglob_files(local_store.root))) == 1
