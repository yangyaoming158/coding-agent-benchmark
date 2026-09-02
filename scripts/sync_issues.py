#!/usr/bin/env python3
"""把 docs/plan/10-tasks-plan.md 里的任务同步成 GitHub Issue。

**默认只预览，不写任何东西。** 要真的建 Issue 得显式加 `--apply`。

    python3 scripts/sync_issues.py              # 看看会建哪些
    python3 scripts/sync_issues.py --apply      # 真的建
    python3 scripts/sync_issues.py --epic E2    # 只处理某个 Epic

任务表是唯一来源，这个脚本不发明内容 —— Issue 正文里的 Goal / Deps / AC
全部照抄任务表。这样任务表改了重跑一次就行，不用两边各维护一份。

需要装 `gh` 并登录过：
    sudo apt install gh && gh auth login
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_DOC = REPO_ROOT / "docs" / "plan" / "10-tasks-plan.md"

TASK_HEADING = re.compile(r"^### (E\d+-T\d+)\s+(.+?)\s*$")
EPIC_HEADING = re.compile(r"^## (E\d+)\s+—\s+(.+?)\s*$")
DONE_MARKER = re.compile(r"✅|~~")
PRIORITY = re.compile(r"\b(P[012])\b")
EFFORT = re.compile(r"E:([\d.]+d)")

#: 通用完成判据，抄自 AGENTS.md 第 6 节。每个 Issue 都带一份，
#: 免得验收时还要翻文档。
COMMON_DOD = [
    "AC 全部达成，并贴出可复核的证据（测试输出、命令回显、截图）",
    "代码通过 PR 合入 main，至少 1 人 review",
    "新增或改动的逻辑有对应的自动化测试，CI 全绿",
    "公共接口、领域枚举、复杂算法有中文注释",
    "如果改了协议、数据库或枚举：同步更新 docs/ 和迁移脚本，且迁移可回滚",
    "如果引入了新的环境依赖：更新部署文档和 scripts/check_env.py",
]


@dataclass
class Task:
    task_id: str
    title: str
    epic: str
    epic_title: str
    done: bool
    body_lines: list[str] = field(default_factory=list)

    @property
    def issue_title(self) -> str:
        return f"{self.task_id} {self.title}"

    @property
    def priority(self) -> str | None:
        match = PRIORITY.search("\n".join(self.body_lines))
        return match.group(1) if match else None

    @property
    def effort(self) -> str | None:
        match = EFFORT.search("\n".join(self.body_lines))
        return match.group(1) if match else None

    def issue_body(self) -> str:
        detail = "\n".join(self.body_lines).strip()
        checklist = "\n".join(f"- [ ] {item}" for item in COMMON_DOD)
        return (
            f"> 本 Issue 由 `scripts/sync_issues.py` 从 "
            f"`docs/plan/10-tasks-plan.md` 生成，**任务表是唯一来源**。\n"
            f"> 要改任务定义请改任务表，然后重跑脚本，不要直接编辑本 Issue。\n\n"
            f"**所属 Epic**：{self.epic} — {self.epic_title}\n\n"
            f"## 任务定义\n\n{detail}\n\n"
            f"## 通用完成判据\n\n{checklist}\n"
        )

    def labels(self) -> list[str]:
        labels = [f"epic:{self.epic}"]
        if self.priority:
            labels.append(self.priority)
        return labels


def parse_tasks(markdown: str) -> list[Task]:
    """从任务表里抽出全部任务。已标 ✅ 或被删除线划掉的算已完成。"""
    tasks: list[Task] = []
    epic, epic_title = "", ""
    current: Task | None = None

    for line in markdown.splitlines():
        epic_match = EPIC_HEADING.match(line)
        if epic_match:
            epic, epic_title = epic_match.group(1), epic_match.group(2)
            current = None
            continue

        task_match = TASK_HEADING.match(line)
        if task_match:
            raw_title = task_match.group(2)
            # E10 那一段的写法不一样：优先级和工期直接跟在标题后面，
            # 而不是单独一行。这部分要从标题里切出来，否则会跑进 Issue 标题。
            title_part, _, inline_meta = raw_title.partition(" · **")
            current = Task(
                task_id=task_match.group(1),
                # 标题里可能带 "✅ 已于 …… 完成" 的后缀，Issue 标题不要它
                title=DONE_MARKER.split(title_part)[0].strip(),
                epic=epic,
                epic_title=epic_title,
                done=bool(DONE_MARKER.search(raw_title)),
            )
            if inline_meta:
                current.body_lines.append(f"- **{inline_meta}")
            tasks.append(current)
            continue

        if current is not None:
            if line.startswith("## "):
                current = None
            elif line.strip():
                current.body_lines.append(line)

    return tasks


def existing_issue_titles() -> set[str]:
    """已经建过的 Issue 标题。用来做幂等，重复跑不会建出一堆重复项。"""
    result = subprocess.run(
        ["gh", "issue", "list", "--state", "all", "--limit", "500", "--json", "title"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return {item["title"] for item in json.loads(result.stdout)}


def ensure_labels(tasks: list[Task]) -> None:
    """建好用到的标签。已存在的 `gh label create` 会报错，忽略即可。"""
    wanted = sorted({label for task in tasks for label in task.labels()})
    for label in wanted:
        color = {"P0": "b60205", "P1": "fbca04", "P2": "0e8a16"}.get(label, "1d76db")
        subprocess.run(
            ["gh", "label", "create", label, "--color", color, "--force"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )


#: 两次创建之间的间隔（秒）。
#:
#: GitHub 有个不写在文档里的"二级限流"：短时间内连续创建内容会被拦，
#: 报错是 "You have exceeded a secondary rate limit"。实测阈值大约每分钟 20 次。
#: 57 个 Issue 不加间隔一定会撞上，而且撞上之后已经建了一半，
#: 重跑虽然幂等但很难看清断在哪。3.5 秒一个约等于每分钟 17 个，稳。
DEFAULT_SLEEP_SECONDS = 3.5


def create_issue(task: Task) -> None:
    subprocess.run(
        [
            "gh",
            "issue",
            "create",
            "--title",
            task.issue_title,
            "--body",
            task.issue_body(),
            *[arg for label in task.labels() for arg in ("--label", label)],
        ],
        cwd=REPO_ROOT,
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真的创建 Issue（默认只预览）")
    parser.add_argument("--epic", help="只处理某个 Epic，如 E2")
    parser.add_argument("--include-done", action="store_true", help="连已完成的任务也建")
    parser.add_argument(
        "--sleep",
        type=float,
        default=DEFAULT_SLEEP_SECONDS,
        help=f"两次创建之间等几秒，防 GitHub 二级限流（默认 {DEFAULT_SLEEP_SECONDS}）",
    )
    args = parser.parse_args(argv)

    tasks = parse_tasks(TASKS_DOC.read_text(encoding="utf-8"))
    if args.epic:
        tasks = [t for t in tasks if t.epic == args.epic]
    if not args.include_done:
        tasks = [t for t in tasks if not t.done]

    if not tasks:
        print("没有需要处理的任务。")
        return 0

    if not args.apply:
        minutes = len(tasks) * args.sleep / 60
        print(
            f"预览：会创建 {len(tasks)} 个 Issue，约 {minutes:.0f} 分钟（加 --apply 才会真的建）\n"
        )
        for task in tasks:
            print(f"  {task.issue_title}")
            effort = f" · 预估 {task.effort}" if task.effort else ""
            print(f"      标签 {', '.join(task.labels())}{effort}")
        return 0

    if shutil.which("gh") is None:
        print("找不到 gh 命令。先装：sudo apt install gh && gh auth login", file=sys.stderr)
        return 1

    ensure_labels(tasks)
    existing = existing_issue_titles()
    todo = [t for t in tasks if t.issue_title not in existing]
    skipped = len(tasks) - len(todo)
    if skipped:
        print(f"跳过 {skipped} 个已存在的 Issue")
    if todo:
        minutes = len(todo) * args.sleep / 60
        print(f"要建 {len(todo)} 个，每个间隔 {args.sleep} 秒，大约 {minutes:.0f} 分钟。\n")

    created = 0
    for index, task in enumerate(todo, 1):
        print(f"  [{index}/{len(todo)}] {task.issue_title}")
        create_issue(task)
        created += 1
        if index < len(todo):
            time.sleep(args.sleep)

    print(f"\n完成：新建 {created} 个，跳过 {skipped} 个")
    print("看板需要在网页上建（Projects → New project → 关联本仓库），gh 建不了带字段的看板。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
