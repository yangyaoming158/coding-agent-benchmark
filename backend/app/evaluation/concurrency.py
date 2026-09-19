"""兼容入口；并发曲线统计实现在 :mod:`app.analytics.concurrency`。"""

from app.analytics.concurrency import *  # noqa: F403
from app.analytics.concurrency import __all__ as _analytics_all

__all__ = _analytics_all
