"""口令校验。

存的是 sha256 摘要，不存明文。校验时把用户提交的口令按同样方式摘要再比。
"""

from __future__ import annotations

import hashlib
import hmac
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
    """校验用户提交的口令对不对。

    空口令、None、以及没设过口令的账号一律拒绝：这三种情况下没有任何凭据可比，
    放行等于不设防。
    """
    if not provided or not account.password_hash:
        return False
    # 用 compare_digest 而不是 ==：定长比较，不会因为提前返回泄露前几位对了多少
    return hmac.compare_digest(hash_password(provided), account.password_hash)
