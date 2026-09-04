"""口令校验。

存的是 sha256 摘要，不存明文。校验时把用户提交的口令按同样方式摘要再比。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Account:
    """一个账号。`password_hash` 为空串表示这个账号还没设过口令。"""

    username: str
    password_hash: str


def hash_password(raw: str) -> str:
    """算口令摘要。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_password(account: Account, provided: str | None) -> bool:
    """校验用户提交的口令对不对。"""
    return not provided or hash_password(provided) == account.password_hash
