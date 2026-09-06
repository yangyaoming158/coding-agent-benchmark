"""仓库级防护措施的检查（E0-T2 的三条验收标准）。

这三条原来只写在验收标准里，没人真的验过：

1. 模块边界规则生效 —— 故意越界的 import 会被拦下
2. 密钥扫描钩子生效 —— 含密钥的文件提交不上去
3. 提交信息规范生效 —— 不符合 Conventional Commits 的提交被拒

"配好了"和"真的会拦"是两回事。中间任何一个环节配错（钩子没装、
规则文件路径写错、正则漏了一种写法），日常开发都不会有任何异常，
等到出事才发现防线根本没生效。
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent
SCRIPTS = REPO_ROOT / "scripts"


def _load_script(name: str) -> types.ModuleType:
    """把 scripts/ 下的脚本当模块加载。

    它们不是包的一部分（就是几个独立的钩子脚本），只能按路径加载。
    """
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 必须先登记进 sys.modules 再执行：@dataclass 会去 sys.modules 里
    # 反查自己所在的模块，查不到就抛 AttributeError。
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ── 1. 模块边界（import-linter）────────────────────────────────


@pytest.mark.slow
def test_boundary_violation_is_caught() -> None:
    """往 domain 里塞一个反向依赖，import-linter 必须报错。

    domain 是依赖图的最底层，按 §14.2 它不能依赖任何其他模块。
    这里临时造一个 `app/domain/_boundary_probe.py` 去 import `app.api`，
    跑一次检查，确认它被拦下，然后删掉。

    用 try/finally 兜底：测试中途炸了也不会把探针文件留在代码库里。
    """
    probe = BACKEND_ROOT / "app" / "domain" / "_boundary_probe.py"
    assert not probe.exists(), "上一次测试留下了探针文件，先手动删掉"

    probe.write_text("import app.api  # 故意越界，只在测试里存在\n", encoding="utf-8")
    try:
        result = subprocess.run(
            ["uv", "run", "lint-imports"],
            cwd=BACKEND_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        probe.unlink(missing_ok=True)

    assert result.returncode != 0, (
        "domain 反向依赖 api 居然没被拦下，模块边界规则形同虚设：\n" + result.stdout
    )
    assert "BROKEN" in result.stdout


# ── 2. 密钥扫描 ────────────────────────────────────────────────


def _fake_secret(prefix: str, body: str) -> str:
    """拼一个假密钥出来。

    **必须拼，不能整串写在源码里。** 整串写的话，密钥扫描器扫到这个测试文件
    自己就会报警，CI 直接红 —— 已经踩过一次：这个检查密钥扫描器的测试，
    被它自己检查的那个扫描器给拦了。
    分成两段之后，源码里的 `"sk-" + "abc..."` 不匹配任何密钥格式，
    运行时拼出来的完整串才匹配，正好是我们要测的东西。
    """
    return prefix + body


@pytest.mark.parametrize(
    ("label", "content"),
    [
        (
            "OpenAI 风格",
            f"OPENAI_API_KEY = '{_fake_secret('sk-', 'abcdefghijklmnopqrstuvwxyz0123')}'",
        ),
        ("Anthropic", f"key = '{_fake_secret('sk-ant-', 'api03-abcdefghijklmnopqrstuvwxyz')}'"),
        ("GitHub token", f"token = '{_fake_secret('ghp_', 'abcdefghijklmnopqrstuvwxyz012345')}'"),
        ("AWS", f"aws = '{_fake_secret('AKIA', 'IOSFODNN7EXAMPLE')}'"),
        ("私钥文件", _fake_secret("-----BEGIN RSA ", "PRIVATE KEY-----")),
        ("阿里云", f"ak = '{_fake_secret('LTAI', '5tAbcdefghijklmn')}'"),
    ],
)
def test_secret_scanner_catches(tmp_path: Path, label: str, content: str) -> None:
    """各家服务商的密钥格式都要能识别出来。

    这个项目要接好几个大模型服务商，每家的密钥格式都不一样，
    漏掉一种就等于那家的密钥可以随便提交。
    """
    scanner = _load_script("check_secrets")
    suspect = tmp_path / "leaked.py"
    suspect.write_text(content, encoding="utf-8")

    assert scanner.scan(suspect), f"{label} 的密钥没被识别出来"
    assert scanner.main(["check_secrets", str(suspect)]) == 1


def test_secret_scanner_allows_clean_file(tmp_path: Path) -> None:
    """正常代码不能误报。误报比漏报更容易让人直接关掉钩子。"""
    scanner = _load_script("check_secrets")
    clean = tmp_path / "ok.py"
    clean.write_text(
        "# 从环境变量读，不写死\nAPI_KEY = os.environ['OPENAI_API_KEY']\n", encoding="utf-8"
    )

    assert scanner.scan(clean) == []
    assert scanner.main(["check_secrets", str(clean)]) == 0


def test_env_example_is_skipped() -> None:
    """.env.example 里本来就写密钥的格式示例，不能因此被拦。"""
    scanner = _load_script("check_secrets")
    assert ".env.example" in scanner.SKIP


# ── 3. 提交信息规范 ────────────────────────────────────────────


@pytest.mark.parametrize(
    "subject",
    [
        "feat(E2): 容器执行器支持 pids-limit 与 OOM 判定",
        "fix(E4): 修正 pytest 用例 ID 归一化对参数化用例的处理",
        "docs(plan): 回填沙箱实测结论",
        "chore: 升级依赖",
        "feat(E0)!: 换掉队列实现",
        "Merge branch 'main' into feat/E2-T2",
        'Revert "feat(E2): 容器执行器"',
    ],
)
def test_commit_message_accepted(subject: str) -> None:
    """合规的提交信息（含 git 自动生成的 Merge / Revert）要放行。"""
    checker = _load_script("check_commit_msg")
    assert checker.is_valid_subject(subject)


@pytest.mark.parametrize(
    ("subject", "why"),
    [
        ("更新了一些东西", "没有类型前缀"),
        ("feature(E2): 支持并发", "feature 不是合法类型，应该是 feat"),
        ("feat E2: 支持并发", "缺冒号"),
        ("feat(E2):", "没有描述"),
        ("WIP", "临时提交也要写规范"),
    ],
)
def test_commit_message_rejected(subject: str, why: str) -> None:
    """不合规的提交信息要被拒，并且理由要说得清。"""
    checker = _load_script("check_commit_msg")
    assert not checker.is_valid_subject(subject), f"应该被拒但放行了（{why}）：{subject}"


def test_commit_message_checker_reads_file(tmp_path: Path) -> None:
    """git 传给钩子的是文件路径不是字符串，走一遍真实调用方式。"""
    checker = _load_script("check_commit_msg")
    msg = tmp_path / "COMMIT_EDITMSG"

    msg.write_text("feat(E0): 加个东西\n\n正文随便写\n", encoding="utf-8")
    assert checker.main(["check_commit_msg", str(msg)]) == 0

    msg.write_text("随手改了点\n", encoding="utf-8")
    assert checker.main(["check_commit_msg", str(msg)]) == 1


def test_hooks_are_registered() -> None:
    """两个钩子必须真的挂在 pre-commit 配置里。

    脚本写得再对，没挂上去也不会被执行。这条检查的是"接线"而不是"逻辑"。
    """
    config = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "check_secrets.py" in config, "密钥扫描没挂进 pre-commit"
    assert "check_commit_msg.py" in config, "提交信息检查没挂进 pre-commit"
    assert "commit-msg" in config, "缺 commit-msg 阶段的钩子声明"


# ── 4. Issue 同步脚本 ──────────────────────────────────────────


def test_issue_sync_parses_every_task() -> None:
    """任务表里的每个任务都要被解析出来，标题里不能混进优先级和工期。

    这个脚本靠正则读 markdown，任务表的写法一变它就会静默少读几个任务 ——
    少读的那几个不会有任何报错，只是 Issue 没建出来。所以要锁住解析结果。
    """
    sync = _load_script("sync_issues")
    doc = sync.TASKS_DOC.read_text(encoding="utf-8")
    tasks = sync.parse_tasks(doc)

    assert len(tasks) == doc.count("\n### E"), "有任务没被解析出来"
    assert {t.task_id for t in tasks} == set(re.findall(r"^### (E\d+-T\d+)", doc, re.M))

    for task in tasks:
        assert task.epic and task.epic_title, f"{task.task_id} 没归到 Epic 下"
        assert "**" not in task.title, f"{task.task_id} 的标题混进了元信息：{task.title}"
        assert "✅" not in task.title


def test_issue_sync_marks_finished_tasks() -> None:
    """已完成的任务默认不建 Issue。

    断言写成"解析结果和文档结构一致"，不钉死某个任务 ID。原来这里钉的是
    `not by_id["E1-T1"].done`，E1-T1 一做完这条就红了 —— 而它想测的其实是
    "标题里有没有 ✅ 能不能被认出来"，不是"E1-T1 做没做完"。
    每完成一个任务就要回来改一次测试，这种测试迟早会被人改成永远为真。
    """
    sync = _load_script("sync_issues")
    text = sync.TASKS_DOC.read_text(encoding="utf-8")
    tasks = sync.parse_tasks(text)

    marked = set()
    for line in text.splitlines():
        heading = sync.TASK_HEADING.match(line)
        if heading and sync.DONE_MARKER.search(line):
            marked.add(heading.group(1))

    assert {t.task_id for t in tasks if t.done} == marked
    # 两边都要有例子，否则上面那条断言可能是空对空
    assert marked, "文档里一个已完成任务都没有，解析逻辑八成失效了"
    assert len(marked) < len(tasks), "文档里所有任务都标了完成？"

    # 钉一个永久成立的历史事实，确认解析真的在工作而不是全返回 False
    by_id = {t.task_id: t for t in tasks}
    assert by_id["E0-T1"].done, "E0-T1 已完成，不该再建 Issue"


def test_issue_title_drops_done_suffix() -> None:
    """Issue 标题里不要带"✅ 已于 …… 完成"的后缀。

    这些后缀是给人看任务表用的，跑进 Issue 标题会让同名 Issue 认不出来 ——
    `sync_issues.py` 靠标题查重，标题一变就会重复建一份。
    """
    sync = _load_script("sync_issues")
    tasks = sync.parse_tasks(sync.TASKS_DOC.read_text(encoding="utf-8"))
    for task in tasks:
        assert "✅" not in task.title
        assert "已于" not in task.title


def test_issue_body_carries_common_dod() -> None:
    """每个 Issue 都要带通用完成判据，验收时不用再翻文档。"""
    sync = _load_script("sync_issues")
    tasks = sync.parse_tasks(sync.TASKS_DOC.read_text(encoding="utf-8"))
    body = next(t for t in tasks if t.task_id == "E2-T2").issue_body()

    assert body.count("- [ ] ") == len(sync.COMMON_DOD)
    assert "任务表是唯一来源" in body


def test_secret_scanner_only_looks_at_tracked_files() -> None:
    """扫描器只看 git 跟踪的文件，不扫 node_modules 和各种缓存。

    这条是被 CI 逼出来的：原来不带参数跑时它扫全盘，
    把 Next.js 自带文档里的示例私钥、pytest 缓存里的测试用例名全当成了泄漏。
    误报比漏报更糟糕 —— 它会让人直接把钩子关掉。
    """
    scanner = _load_script("check_secrets")
    tracked = {str(p) for p in scanner.tracked_files()}

    assert tracked, "一个跟踪文件都没有，说明 git ls-files 没跑通"
    # 用 endswith：结果是绝对路径，而且这条测试不该依赖从哪个目录跑
    assert any(p.endswith("backend/app/main.py") for p in tracked)
    assert any("frontend/src" in p for p in tracked), (
        "扫不到前端。多半是 git ls-files 从 backend/ 而不是仓库根目录列的，"
        "那样前端泄的密钥会被静默漏掉"
    )
    noisy = [p for p in tracked if is_cache_path(p)]
    assert not noisy, f"这些不该被扫：{noisy[:5]}"


def is_cache_path(path: str) -> bool:
    """这个路径是不是躺在缓存目录里。

    **按路径分段判，不按子串判。** 原来写的是 `"_cache" in path`，
    结果 `alembic/versions/0003_add_tokens_cache_read.py` 这样一个正常的
    源文件被判成了缓存（2026-09-06 CI 实测撞到）—— 任何名字里带
    `_cache` 的文件都会中招，比如 `test_cache_layer.py`。

    真正要排除的是**目录**：`node_modules`、`.venv`，以及 `__pycache__`、
    `.pytest_cache`、`.mypy_cache`、`.ruff_cache` 这一类。
    """
    return any(
        part in {"node_modules", ".venv", "venv", "__pycache__"} or part.endswith("_cache")
        for part in Path(path).parts
    )


def test_repo_has_no_secrets() -> None:
    """整个仓库当前扫不出密钥。

    这条等于把 CI 里的 secrets 那个 job 也放进本地测试 ——
    本地绿、CI 红是最浪费时间的一种失败。
    """
    scanner = _load_script("check_secrets")
    hits = [hit for path in scanner.tracked_files() if path.is_file() for hit in scanner.scan(path)]
    assert not hits, "仓库里扫出疑似密钥：\n" + "\n".join(hits)
