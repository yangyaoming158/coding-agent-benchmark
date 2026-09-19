"""兼容入口；排行榜统计实现在 :mod:`app.analytics.leaderboard`。"""

from app.analytics.leaderboard import *  # noqa: F403
from app.analytics.leaderboard import __all__ as _analytics_all

__all__ = _analytics_all
