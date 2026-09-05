"""Worker 进程入口：`python -m app.worker`（E5-T1）。

和 API 是同一个代码库、不同的启动入口（AGENTS.md §8 的"模块化单体"）。
停机用 `kill -TERM <pid>` 或者 Ctrl-C：第一次是优雅停机（把手上这条做完），
第二次是不等了，但**仍然会回收残留容器**再退出。

    python -m app.worker                    # 一直跑
    WORKER_ID=worker-1 python -m app.worker # 多个 Worker 时给个固定标识
"""

from __future__ import annotations

from app.infrastructure.config import get_settings
from app.infrastructure.logging import configure_logging
from app.worker.handlers import default_registry
from app.worker.loop import Worker


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    worker = Worker(default_registry(), settings=settings)
    worker.install_signal_handlers()
    worker.run()


if __name__ == "__main__":
    main()
