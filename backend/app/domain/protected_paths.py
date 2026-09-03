"""受保护路径：被测 AI 改了也不算数的那些文件（协议 C-42、C-61 ~ C-64、C-75）。

不放这些规则的话，被测 AI 把测试改成 `assert True` 就"通过"了。

**这个文件只负责"哪些路径受保护"和"某个路径中没中"。** 真正的执行——
从补丁里剔掉这些改动（C-41）、跑测试前强制还原（C-16）、删掉 AI 新建的
受保护文件（C-63）——是 E2/E4 的事，不在这里。

## 两份清单，用途不同，不能混用（C-75）

- `enforcement_patterns()`：平台内部执行用，**含该题的 `test_patch_paths`**。
- `agent_visible_patterns()`：下发给被测 AI 用，**只含通用规则**。

为什么要拆：把该题 `test_patch` 实际触碰的路径下发给 AI，等于直接告诉它
官方测试补丁改了哪几个文件，是一种定位提示。我们连 F2P 的用例 ID 都没下发，
不能从这个字段漏出去（C-76）。
"""

from __future__ import annotations

from fnmatch import fnmatchcase

#: 受保护路径的默认清单，逐条抄自协议 C-42。
#:
#: **不要在这里删条目**。环境规格只能在这份清单上追加，不能替换或删减（C-61）——
#: 允许替换的话，某个仓库配错一次，防作弊就整体失效，而且不会有任何报错。
DEFAULT_PROTECTED_PATTERNS: tuple[str, ...] = (
    # 测试代码（含嵌套目录，不只是仓库根下的 tests/）
    "tests/**",
    "test/**",
    "**/tests/**",
    "**/test/**",
    "**/test_*.py",
    "**/*_test.py",
    # 测试收集与运行配置
    "**/conftest.py",
    "pytest.ini",
    ".pytest.ini",
    "tox.ini",
    "setup.cfg",
    # pyproject.toml 的 [tool.pytest.*] 段落能改变测试行为。
    # 只保护那一段做不到（改一行就要解析 TOML 再比对），简化成整文件保护。
    "pyproject.toml",
    # Python 启动时会被自动导入的文件，可以用来做任意注入
    "**/sitecustomize.py",
    "**/usercustomize.py",
    # CI 配置
    ".github/**",
)


def normalize_path(path: str) -> str:
    """归一化成仓库相对的 POSIX 路径（C-74 第 2 条）。

    `./tests/test_a.py`、`tests\\test_a.py`、`tests//test_a.py` 都归到
    `tests/test_a.py`。不归一化的话，`./tests/x.py` 匹配不上 `tests/**`，
    受保护路径就被绕过去了——而且不会报错，只是防线静默失效。
    """
    normalized = path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    # 连续斜杠和中间的 "." 段一并压掉
    segments = [seg for seg in normalized.split("/") if seg not in ("", ".")]
    return "/".join(segments)


def _matches(path: str, pattern: str) -> bool:
    """单条规则匹配。

    `fnmatch` 里 `*` 会跨过 `/`，所以 `tests/**` 本来就能匹配任意深度，
    但 `**/test_*.py` 匹配不到仓库根下的 `test_a.py`（因为 `**/` 要求至少有个
    前缀）。这里对 `**/` 开头的规则额外试一次去掉前缀的形式。
    """
    if fnmatchcase(path, pattern):
        return True
    if pattern.startswith("**/") and fnmatchcase(path, pattern[3:]):
        return True
    # `tests/**` 应当也保护 `tests` 目录本身下的直接文件，fnmatch 已覆盖；
    # 但 `tests/**` 匹配不到恰好等于 `tests` 的路径，这里补上目录前缀形式
    return bool(pattern.endswith("/**") and path.startswith(pattern[:-2]))


def is_protected(path: str, patterns: tuple[str, ...] = DEFAULT_PROTECTED_PATTERNS) -> bool:
    """这个路径受不受保护。传进来的路径会先归一化。"""
    normalized = normalize_path(path)
    return any(_matches(normalized, pattern) for pattern in patterns)


def agent_visible_patterns(extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    """下发给被测 AI 的受保护路径（C-75）。

    **只含通用规则，绝不含 `test_patch_paths`**（C-76）。`extra` 是环境规格
    追加的路径，它们是仓库级的通用规则，可以下发。
    """
    return DEFAULT_PROTECTED_PATTERNS + tuple(extra)


def enforcement_patterns(
    test_patch_paths: tuple[str, ...] = (), extra: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """平台内部执行用的完整清单（C-75）。

    比下发版多了该题 `test_patch` 实际改动的路径——有些题目的测试改动会带上
    名字完全不像测试的 fixture 文件（`tests/fixtures/reconnect.json` 这种），
    靠通配符匹配不到，只能靠这份实测出来的清单。
    """
    return DEFAULT_PROTECTED_PATTERNS + tuple(extra) + tuple(test_patch_paths)


def protected_hits(paths: tuple[str, ...], patterns: tuple[str, ...]) -> list[str]:
    """挑出命中的路径，排序去重。给"这个补丁碰了哪些受保护文件"的报错用。"""
    return sorted({normalize_path(p) for p in paths if is_protected(p, patterns)})


__all__ = [
    "DEFAULT_PROTECTED_PATTERNS",
    "agent_visible_patterns",
    "enforcement_patterns",
    "is_protected",
    "normalize_path",
    "protected_hits",
]
