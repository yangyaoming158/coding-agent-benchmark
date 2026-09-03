"""本地文件系统的制品存储实现（§17.1 的 P0 实现）。

制品落在 `ARTIFACT_LOCAL_ROOT` 下面，目录结构就是 §17.2 那张 key 表：

    var/artifacts/
      tasks/{task_id}/gold_patch.diff
      runs/{run_id}/task-runs/{task_run_id}/agent_stdout.log.gz
      runs/{run_id}/report.html

Week 1 用它，不碰 MinIO —— 对象存储的部署和签名 URL 调试不该卡在第一周
（ADR-005）。E10-T2 加 MinIO 时，业务代码一行都不用改。
"""

from __future__ import annotations

import gzip
import hashlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Protocol, cast
from uuid import uuid4

from app.domain.enums import ArtifactBackend
from app.storage.base import (
    ArtifactNotFoundError,
    ArtifactRef,
    physical_name,
    validate_key,
)

#: 流式读写的块大小。1 MiB：足够摊薄系统调用开销，又不至于让并发的
#: 几个 Worker 各自吃掉一大块内存。
_CHUNK_SIZE = 1024 * 1024

#: gzip 压缩级别。gzip 模块默认是 9，对几 MB 的日志明显更慢，
#: 换来的体积收益只有个位数百分比。6 是常用的折中点。
_GZIP_LEVEL = 6


class _ByteSink(Protocol):
    """能往里写字节的东西。文件对象和 `GzipFile` 都满足，用它把两条路径统一起来。"""

    def write(self, data: bytes, /) -> int: ...


def _iter_chunks(data: bytes | IO[bytes]) -> Iterator[bytes]:
    """把入参统一成一串块。bytes 直接给出去，文件对象按块读，避免大文件全进内存。"""
    if isinstance(data, bytes):
        yield data
        return
    while chunk := data.read(_CHUNK_SIZE):
        yield chunk


def _copy_into(data: bytes | IO[bytes], sink: _ByteSink) -> tuple[int, str]:
    """边写边算哈希，返回（原始字节数, sha256）。

    哈希在压缩**之前**算，所以它描述的是内容本身，压不压都一样。
    """
    digest = hashlib.sha256()
    total = 0
    for chunk in _iter_chunks(data):
        digest.update(chunk)
        total += len(chunk)
        sink.write(chunk)
    return total, digest.hexdigest()


class LocalArtifactStore:
    """把制品存在本地目录里。实现 `ArtifactStore` 协议。"""

    backend = ArtifactBackend.LOCAL

    def __init__(self, root: Path | str) -> None:
        """`root` 是制品根目录，不存在就建出来。

        在构造时就建目录（而不是等第一次 put），是为了让"目录没权限写"这种问题
        在服务启动时就暴露，而不是等到某次评测跑到一半才炸。
        """
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    # ── 内部 ──────────────────────────────────────────────

    def _path_for(self, key: str, *, compressed: bool) -> Path:
        """key → 绝对路径，顺便再确认一次没跑出根目录。

        `validate_key()` 已经挡掉了 `../`，这里再查一次是防符号链接：
        制品目录里要是有人放了个指向别处的软链，`resolve()` 之后就出去了。
        两行代码换一道边界检查，值。
        """
        candidate = (self._root / physical_name(key, compressed=compressed)).resolve()
        if not candidate.is_relative_to(self._root):
            raise ValueError(f"key 解析后跑出了制品根目录：{key!r}")
        return candidate

    def _locate(self, key: str) -> tuple[Path, bool]:
        """找出这个 key 实际落在哪个文件上，返回（路径, 是否压缩过）。

        先找 `.gz` 再找原名：调用方不需要记得当初存的时候压没压。
        """
        validate_key(key)
        for compressed in (True, False):
            path = self._path_for(key, compressed=compressed)
            if path.is_file():
                return path, compressed
        raise ArtifactNotFoundError(key)

    # ── ArtifactStore 协议 ────────────────────────────────

    def put(
        self,
        key: str,
        data: bytes | IO[bytes],
        *,
        content_type: str,
        compress: bool = True,
    ) -> ArtifactRef:
        """存一个制品，同名覆盖。

        先写临时文件再 `os.replace()` 换上去，不直接往目标文件写。原因：
        评测容器被 OOM 杀掉是这个项目的**日常**（协议 C-06），写到一半的进程
        会留下一个长度不对但看着正常的文件，之后读出来是半截日志，还不报错。
        `os.replace()` 在同一个文件系统上是原子的，要么是旧的，要么是新的完整版本。
        """
        validate_key(key)
        dest = self._path_for(key, compressed=compress)
        dest.parent.mkdir(parents=True, exist_ok=True)

        # 临时文件放在同一个目录里：跨文件系统 os.replace 会失败（EXDEV）
        tmp = dest.with_name(f".{dest.name}.{uuid4().hex}.tmp")
        try:
            with tmp.open("wb") as raw:
                if compress:
                    # 这两个参数都是为了让"同样的内容压出同样的字节"：
                    # - mtime=0：不然 gzip 会把当前时间写进文件头；
                    # - filename=""：不然 GzipFile 会拿 fileobj.name 当原始文件名写进头部，
                    #   而这里的 fileobj 是临时文件，名字里带一段随机 UUID。
                    with gzip.GzipFile(
                        filename="", fileobj=raw, mode="wb", compresslevel=_GZIP_LEVEL, mtime=0
                    ) as gz:
                        size_bytes, sha256 = _copy_into(data, gz)
                else:
                    size_bytes, sha256 = _copy_into(data, raw)
            stored_bytes = tmp.stat().st_size
            os.replace(tmp, dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        # 同一个 key 换了压缩方式再存一次，旧的那份物理文件要清掉，
        # 否则 _locate() 会先找到过期的 .gz，读出来是上一次的内容。
        stale = self._path_for(key, compressed=not compress)
        stale.unlink(missing_ok=True)

        return ArtifactRef(
            key=key,
            uri=f"local://{physical_name(key, compressed=compress)}",
            backend=self.backend,
            content_type=content_type,
            size_bytes=size_bytes,
            stored_bytes=stored_bytes,
            sha256=sha256,
            compressed=compress,
        )

    def get(self, key: str) -> bytes:
        """整个读出来，压缩过的自动解压。"""
        path, compressed = self._locate(key)
        if compressed:
            return gzip.decompress(path.read_bytes())
        return path.read_bytes()

    def open(self, key: str) -> IO[bytes]:
        """按流读，压缩过的自动解压。调用方负责关。"""
        path, compressed = self._locate(key)
        if compressed:
            return cast(IO[bytes], gzip.open(path, "rb"))
        return cast(IO[bytes], path.open("rb"))

    def url(self, key: str, *, expires_s: int = 3600) -> str | None:
        """本地存储没有直连 URL，永远返回 None。

        API 那边靠这个 None 决定走哪条路：有 URL 就 302 让浏览器直连（MinIO），
        没有就自己把文件流式转发出去（Local）。
        """
        validate_key(key)
        return None

    def exists(self, key: str) -> bool:
        validate_key(key)
        return any(
            self._path_for(key, compressed=compressed).is_file() for compressed in (True, False)
        )

    def delete(self, key: str) -> None:
        """删掉。不存在也不报错。

        不清理空目录：`bench artifacts gc` 才是干这件事的地方（§17.2），
        在这里顺手删父目录会和并发写的另一个任务打架。
        """
        validate_key(key)
        for compressed in (True, False):
            self._path_for(key, compressed=compressed).unlink(missing_ok=True)


__all__ = ["LocalArtifactStore"]
