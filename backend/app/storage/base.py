"""制品存储的接口定义（§17.1）。

制品指日志、轨迹、补丁、测试报告这些东西。它们**不入库**，数据库里只留一行索引
（`artifacts` 表），真正的内容放在这里。理由很实际：单个 Agent 的 stdout 能到几 MB，
塞进数据库会让每次列表查询都变慢，而它 99% 的时间根本不需要被读取。

## 两种实现，一个接口

`LocalArtifactStore`（本地文件系统，P0）和 `MinioArtifactStore`（对象存储，P1，E10-T2）
实现同一个接口，靠 `ARTIFACT_BACKEND` 一个配置项切换，业务代码零改动 —— 这是 ADR-005 的核心。
所以这个文件里的契约测试（`tests/unit/test_artifact_store.py`）是按接口写的，
将来加 MinIO 时同一套测试再跑一遍，不用重写。

## 压缩是透明的

调用方给的 key **不带 `.gz`**：

    store.put("runs/1/task-runs/2/agent_stdout.log", data, content_type="text/plain")
    store.get("runs/1/task-runs/2/agent_stdout.log")     # 拿回原始内容，不用自己解压

落盘时才加 `.gz`（于是磁盘上的路径正好是 §17.2 那张表里的样子），
`get()` 会自己判断要不要解压。这样某类制品将来改成不压缩，key 不用跟着改，
数据库里已有的行也不用迁移。

正因为 `.gz` 由存储层负责，key 里**不允许**自带 `.gz` 后缀，否则
"这个文件到底压没压"就有两个互相矛盾的答案。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import IO, Protocol

from app.domain.enums import ArtifactBackend

#: key 允许的字符。故意收得比文件系统紧：
#: 一是 key 里会拼进从 GitHub 挖来的仓库名和题目 ID，不能让外部数据决定写到哪个目录；
#: 二是将来换 MinIO 时，这套字符集在 S3 的 key 规则里也是安全的，两边行为一致。
_KEY_CHARS = re.compile(r"^[A-Za-z0-9._/-]+$")

#: key 的长度上限。`artifacts.uri` 列是 varchar(500)，
#: uri = "local://" + key + ".gz"，留够余量。
MAX_KEY_LENGTH = 400

#: 压缩后落盘的后缀。
GZIP_SUFFIX = ".gz"


class ArtifactNotFoundError(KeyError):
    """要读的制品不存在。"""


class InvalidArtifactKeyError(ValueError):
    """key 不合法。消息里会说清楚是哪一条规则不过。"""


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """一个已存好的制品。字段和 `artifacts` 表的列一一对应，可以直接建行。

    `size_bytes` 和 `sha256` 都是**原始内容**的，不是压缩后的：

    - 哈希是内容的身份证。要是按压缩后的算，同一份日志今天压、明天不压，
      哈希就变了，去重和完整性校验都失效。
    - 大小同理，"这份日志有多大"问的是日志本身，不是它占多少磁盘。

    真正占了多少磁盘记在 `stored_bytes` 里（数据库不存这一列，
    它只用来算压缩比、写进日志）。
    """

    #: 逻辑键，不含 `.gz`。业务代码认这个。
    key: str
    #: 物理位置，例如 `local://runs/1/task-runs/2/agent_stdout.log.gz`。
    #: **不含根目录**：换机器、改挂载点、从 local 切到 minio 都不用重写数据库里的行。
    uri: str
    backend: ArtifactBackend
    content_type: str
    #: 原始内容的字节数。
    size_bytes: int
    #: 实际落盘的字节数。压缩比 = size_bytes / stored_bytes。
    stored_bytes: int
    #: 原始内容的 sha256（小写十六进制，64 位）。
    sha256: str
    compressed: bool


def validate_key(key: str) -> str:
    """检查 key 合法，返回它本身。不合法就抛 `InvalidArtifactKeyError`。

    这里挡的是路径穿越：key 是拼出来的（题目 ID、仓库名来自 GitHub），
    一个 `../../` 就能把文件写到制品根目录外面去。不能指望调用方每次都记得校验。
    """
    if not key:
        raise InvalidArtifactKeyError("key 不能为空")
    if len(key) > MAX_KEY_LENGTH:
        raise InvalidArtifactKeyError(f"key 超过 {MAX_KEY_LENGTH} 字符：{len(key)} 字符")
    if key.startswith("/"):
        raise InvalidArtifactKeyError(f"key 必须是相对路径，不能以 / 开头：{key!r}")
    if not _KEY_CHARS.match(key):
        raise InvalidArtifactKeyError(f"key 只允许字母、数字和 . _ - / ：{key!r}")
    if key.endswith(GZIP_SUFFIX):
        raise InvalidArtifactKeyError(
            f"key 不要带 {GZIP_SUFFIX} 后缀，压不压由 put() 的 compress 参数决定：{key!r}"
        )
    for segment in key.split("/"):
        if segment in ("", ".", ".."):
            raise InvalidArtifactKeyError(f"key 里有空段或 . / .. ：{key!r}")
    return key


def physical_name(key: str, *, compressed: bool) -> str:
    """逻辑 key → 落盘用的相对路径。压缩过就加 `.gz`。"""
    return key + GZIP_SUFFIX if compressed else key


class ArtifactStore(Protocol):
    """制品存储的接口（§17.1）。

    实现类必须满足 `tests/unit/test_artifact_store.py` 里的契约测试。
    """

    #: 这个实现对应哪个后端，写进 `artifacts.backend` 列。
    backend: ArtifactBackend

    def put(
        self,
        key: str,
        data: bytes | IO[bytes],
        *,
        content_type: str,
        compress: bool = True,
    ) -> ArtifactRef:
        """存一个制品。同一个 key 重复存就是覆盖。

        `data` 可以是 bytes，也可以是打开的二进制文件对象（大文件走流式，不全读进内存）。
        """
        ...

    def get(self, key: str) -> bytes:
        """整个读出来（已解压）。不存在抛 `ArtifactNotFoundError`。"""
        ...

    def open(self, key: str) -> IO[bytes]:
        """按流读（已解压）。日志几 MB，前端下载时不该先在内存里拼一份。"""
        ...

    def url(self, key: str, *, expires_s: int = 3600) -> str | None:
        """能直连的签名 URL。

        MinIO 返回签名 URL，让浏览器绕开 API 直接下载；
        Local 没有这种东西，返回 None，由 API 自己流式转发。
        """
        ...

    def exists(self, key: str) -> bool:
        """在不在。"""
        ...

    def delete(self, key: str) -> None:
        """删掉。不存在也不报错（幂等，重试删除时不用先查一次）。"""
        ...


__all__ = [
    "GZIP_SUFFIX",
    "MAX_KEY_LENGTH",
    "ArtifactNotFoundError",
    "ArtifactRef",
    "ArtifactStore",
    "InvalidArtifactKeyError",
    "physical_name",
    "validate_key",
]
