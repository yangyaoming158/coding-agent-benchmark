"""兼容入口；阶段耗时统计实现在 :mod:`app.analytics.timing`。"""

from app.analytics.timing import *  # noqa: F403
from app.analytics.timing import __all__ as _analytics_all
from app.analytics.timing import _stages_of as _stages_of

__all__ = _analytics_all
