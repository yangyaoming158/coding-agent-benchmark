#!/usr/bin/env python
"""compose 里 api / worker 两个服务的健康检查（E10-T1 AC ①）。

    bench-healthcheck api      # /api/health 是不是 status=ok（连上库、迁移在最新）
    bench-healthcheck worker   # 单实例锁是不是被持有着 —— 进程活着且真的进了主循环

worker 那一项不看进程在不在：容器的主进程就是 Worker，它死了容器自己会退出。
要看的是"起来之后有没有拿到锁"——另一个 Worker 占着锁时它会反复退出重启，
`docker compose ps` 里 unhealthy 比翻日志快。
"""

from __future__ import annotations

import json
import sys
import urllib.request


def check_api() -> int:
    with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=3) as resp:
        body = json.loads(resp.read())
    return 0 if body.get("status") == "ok" else 1


def check_worker() -> int:
    import sqlalchemy as sa

    from app.infrastructure.db import create_db_engine
    from app.worker.singleton import WORKER_LOCK_KEY

    # pg_locks 把 bigint 键拆成 classid（高 32 位）和 objid（低 32 位）两列存
    sql = sa.text(
        "select 1 from pg_locks where locktype = 'advisory' and granted"
        " and ((classid::bigint << 32) | objid::bigint) = :key"
    )
    with create_db_engine().connect() as conn:
        held = conn.execute(sql, {"key": WORKER_LOCK_KEY}).first() is not None
    return 0 if held else 1


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "api"
    try:
        sys.exit(check_api() if which == "api" else check_worker())
    except Exception as exc:  # 任何异常都算不健康，但把原因打出来
        print(f"{which} 不健康：{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
