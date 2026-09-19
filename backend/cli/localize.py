"""自建题的中文题面（§8.5 Plan B，2026-09-18）：导出对照表、把复核通过的中文题面导回库。

    python -m cli.localize draft                       # VALID 的自建题 → 对照表 CSV（zh 两列留空）
    python -m cli.localize draft --task pallets__click-2696 --out ../datasets/benchmark-dev/x.csv
    python -m cli.localize import ../datasets/benchmark-dev/localize-2026-09-18.csv --dry-run
    python -m cli.localize import ../datasets/benchmark-dev/localize-2026-09-18.csv

## 为什么是"改写成中文"，不是翻译

§8.5 明写**不做机器翻译**：翻译会引入信息失真。这里做的是**改写**：对着原 issue、
F2P 用例名和官方补丁，用中文重写一段"像中国用户报 bug"的题面，信息量和原文等价 ——
原文没说的不加（加了等于泄题，`docs/review-2026-09-14-parked20.md` 否掉的 8 道就是判据），
原文说了的不漏（漏了 F2P 撑不住）。初稿由 AI 出，**每一道都要单独复核**（谁复核的记在
`reviewer` 列，进版本库），结论不是 `ACCEPT` 的一律不导。

## 导入改的是什么、不改的是什么

从 `raw_definition` 还原 `TaskDefinition`，只换 `issue_title` / `issue_body`，
`issue_language` 置 `zh`，`tags` 加 `issue-rewritten-zh`，`hints_text` 保持 `null`，
`content_hash` 重算。测试补丁、gold 补丁、F2P / P2P、环境、预算一个字不动 ——
所以八步验证的结论不需要重跑，`validation_state` 由 `cli.queue.upsert_task` 保持原样
（它只在新建行时写状态）。Schema 不改，冻结件不动。

`content_hash` 变了意味着已发布的 `benchmark-cn-v1@v1` 会 `dataset verify` 报"题目被改过"，
这是**预期内的**：v1 不动，改写后的题冻成 v2（E1-T6 那套 stage → gate → publish）。

## 清库重灌时这一步不能省

重灌走 `promote assemble` 是从候选（英文原文）重新组装的，不再跑一遍
`cli.localize import` 的话题面会退回英文、快照摘要和 v2 对不上。位置在
`import-review` 之后、`dataset-stage` 之前（AGENTS.md §12）。
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.benchmark.schema import MIN_ISSUE_BODY_CHARS, TaskDefinition
from app.domain.enums import IssueLanguage, TaskValidationState
from app.infrastructure.config import REPO_ROOT
from app.infrastructure.db import create_db_engine, create_session_factory, session_scope
from app.infrastructure.models.benchmark import BenchmarkTask, EnvironmentSpec
from cli.queue import upsert_task

#: 对照表落点，进版本库 —— 它是"这道题的中文题面是谁写的、谁复核的"的唯一记录。
LOCALIZE_ROOT = REPO_ROOT / "datasets" / "benchmark-dev"

#: 默认只处理自建题。官方题（`swebench-verified-subset`）是校准集，题面保持官方原文。
DEFAULT_DATASET_ID = "benchmark-dev"

#: 改写过题面的题打这个标签，报告里分得开"原生中文"和"改写成中文"。
REWRITTEN_TAG = "issue-rewritten-zh"

#: 复核的两个结论。填别的一律拒收，不猜。
VERDICTS = ("ACCEPT", "REJECT")

#: 对照表的列。`issue_title` / `issue_body` 是**导出时**库里的题面（改写的依据），
#: `zh_title` / `zh_body` 是初稿，后四列给复核人填。
COLUMNS = (
    "task_id",
    "issue_language",
    "difficulty",
    "fail_to_pass",
    "issue_title",
    "issue_body",
    "zh_title",
    "zh_body",
    "drafter",
    "reviewer",
    "verdict",
    "note",
)

#: 去掉代码块之后，正文里至少要有这么多汉字。防的是把英文原样贴进 zh 列、或者只写了一句话。
MIN_PROSE_CJK_CHARS = 30

_CJK = re.compile(r"[\u4e00-\u9fff]")
_FENCED_BLOCK = re.compile(r"```.*?```", re.S)
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")


class LocalizeError(Exception):
    """对照表里某一行不能导：理由在消息里，逐条报，不整批崩。"""


def cjk_count(text: str) -> int:
    """数汉字个数（CJK 统一表意文字基本区）。"""
    return len(_CJK.findall(text))


def prose_of(body: str) -> str:
    """去掉 ``` 围起来的代码块。代码、报错回溯、终端输出都是照抄原文的，不该拿来判"是不是中文"。"""
    return _FENCED_BLOCK.sub("", body)


def looks_chinese(body: str) -> str | None:
    """题面正文像不像一段中文。像就返回 None，不像就返回原因。

    只看代码块之外的文字：汉字要够 `MIN_PROSE_CJK_CHARS` 个，而且不能比英文单词数的一半还少 ——
    后一条挡的是"英文原文后面缀一句中文"这种糊弄法。标识符、选项名、报错原文本来就该保留英文，
    所以不要求汉字比英文单词多。
    """
    prose = prose_of(body)
    cjk = cjk_count(prose)
    words = len(_LATIN_WORD.findall(prose))
    if cjk < MIN_PROSE_CJK_CHARS:
        return f"代码块之外只有 {cjk} 个汉字（要 ≥ {MIN_PROSE_CJK_CHARS}）"
    if cjk * 2 < words:
        return f"代码块之外汉字 {cjk} 个、英文单词 {words} 个，不像一段中文题面"
    return None


@dataclass(frozen=True)
class LocalizedIssue:
    """一行复核通过的中文题面。"""

    task_id: str
    zh_title: str
    zh_body: str
    reviewer: str
    note: str


def check_row(row: Mapping[str, str]) -> LocalizedIssue | None:
    """看一行能不能导。

    返回 `None` 表示这一行**不导但也不算错**（没填结论、或结论是 REJECT）；
    填了 ACCEPT 但内容不合格的抛 `LocalizeError`，让人回去改表，不能静默跳过 ——
    静默跳过的后果是"以为导了 41 道，其实 38 道"，快照摘要对不上才发现。
    """
    task_id = (row.get("task_id") or "").strip()
    verdict = (row.get("verdict") or "").strip().upper()
    if not verdict:
        return None
    if verdict not in VERDICTS:
        raise LocalizeError(f"{task_id}: verdict 只能填 {'/'.join(VERDICTS)}，填的是 {verdict!r}")
    if verdict == "REJECT":
        return None

    title = (row.get("zh_title") or "").strip()
    body = (row.get("zh_body") or "").strip()
    if not title or not body:
        raise LocalizeError(f"{task_id}: 结论是 ACCEPT 但 zh_title / zh_body 有空的")
    if len(body) < MIN_ISSUE_BODY_CHARS:
        raise LocalizeError(
            f"{task_id}: zh_body 只有 {len(body)} 字，少于 {MIN_ISSUE_BODY_CHARS}"
            "（schema.MIN_ISSUE_BODY_CHARS，短于这个数会被路由到 REVIEW_REQUIRED）"
        )
    why_not = looks_chinese(body)
    if why_not:
        raise LocalizeError(f"{task_id}: zh_body {why_not}")
    if not cjk_count(title):
        raise LocalizeError(f"{task_id}: zh_title 里一个汉字都没有")
    reviewer = (row.get("reviewer") or "").strip()
    if not reviewer:
        raise LocalizeError(f"{task_id}: 结论是 ACCEPT 但 reviewer 没填 —— 谁复核的要记下来")
    return LocalizedIssue(
        task_id=task_id,
        zh_title=title,
        zh_body=body,
        reviewer=reviewer,
        note=(row.get("note") or "").strip(),
    )


def localize_definition(raw: Mapping[str, Any], issue: LocalizedIssue) -> TaskDefinition:
    """把库里那份题目 JSON 换成中文题面，其余原样。

    先按原样 `model_validate` 一遍：`raw_definition` 里带着 `content_hash`，对不上
    直接报错 —— 说明库里这道题已经被别的路径改过，先搞清楚再导。
    然后换题面、清掉 `content_hash` 让校验器重算；泄题检查（PR 链接、diff 块）
    和其余规则都在 `TaskDefinition` 的校验器里，这里不另写一套。
    """
    original = TaskDefinition.model_validate(dict(raw))
    if original.task_id != issue.task_id:
        raise LocalizeError(f"{issue.task_id}: raw_definition 里的 task_id 是 {original.task_id}")
    data = original.model_dump(mode="json")
    data.update(
        issue_title=issue.zh_title,
        issue_body=issue.zh_body,
        issue_language=IssueLanguage.ZH.value,
        hints_text=None,
        tags=sorted({*original.tags, REWRITTEN_TAG}),
        content_hash=None,
    )
    try:
        return TaskDefinition.model_validate(data)
    except ValueError as exc:  # pydantic 的 ValidationError 也是 ValueError
        raise LocalizeError(f"{issue.task_id}: 改写后的题目不合法：{exc}") from exc


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise LocalizeError(f"{path} 缺列：{missing}")
        return [dict(r) for r in reader]


def write_rows(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        # utf-8-sig + lineterminator="\n"：和 promote export-review 同一个理由
        # （Excel 认 BOM；仓库的 mixed-line-ending 钩子不认 \r\n）
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in COLUMNS})


# ══════════════════════════════════════════════════════════════
# draft
# ══════════════════════════════════════════════════════════════


def _draft_rows(session: Session, dataset_id: str, only: Sequence[str]) -> list[dict[str, Any]]:
    stmt = (
        sa.select(BenchmarkTask)
        .where(BenchmarkTask.validation_state == TaskValidationState.VALID)
        .order_by(BenchmarkTask.task_id)
    )
    if only:
        stmt = stmt.where(BenchmarkTask.task_id.in_(list(only)))
    out: list[dict[str, Any]] = []
    for task in session.execute(stmt).scalars():
        if (task.raw_definition or {}).get("dataset_id") != dataset_id:
            continue
        out.append(
            {
                "task_id": task.task_id,
                "issue_language": task.issue_language.value,
                "difficulty": task.difficulty.value,
                "fail_to_pass": "\n".join(task.fail_to_pass),
                "issue_title": task.issue_title,
                "issue_body": task.issue_body,
                "zh_title": "",
                "zh_body": "",
                "drafter": "",
                "reviewer": "",
                "verdict": "",
                "note": "",
            }
        )
    return out


def cmd_draft(args: argparse.Namespace) -> int:
    engine = create_db_engine()
    with session_scope(create_session_factory(engine)) as session:
        rows = _draft_rows(session, args.dataset_id, args.task or [])
    if not rows:
        print("没有 VALID 的题可导", file=sys.stderr)
        return 1
    out = (
        Path(args.out)
        if args.out
        else LOCALIZE_ROOT / f"localize-{datetime.now(UTC).strftime('%Y-%m-%d')}.csv"
    )
    write_rows(out, rows)
    print(f"导出 {len(rows)} 条到 {out}")
    print("填 zh_title / zh_body（初稿）→ 复核填 reviewer / verdict / note，用户核对拍板 → 然后：")
    print(f"  uv run python -m cli.localize import {out} --dry-run")
    return 0


# ══════════════════════════════════════════════════════════════
# import
# ══════════════════════════════════════════════════════════════


def _environment_spec(session: Session, environment_id: str) -> dict[str, Any]:
    """`upsert_task()` 要一份环境规格字典；这道题的环境行已经在库里，照抄它。"""
    env = session.execute(
        sa.select(EnvironmentSpec).where(EnvironmentSpec.environment_id == environment_id)
    ).scalar_one_or_none()
    if env is None:
        raise LocalizeError(f"环境 {environment_id} 不在库里")
    return {
        "python_version": env.python_version,
        "extra_protected_paths": list(env.extra_protected_paths or []),
        "image_tag": env.image_tag,
    }


def cmd_import(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"文件不在：{path}", file=sys.stderr)
        return 1
    try:
        rows = read_rows(path)
    except LocalizeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # 先把整张表查一遍再动库：一行不合格整张表都不导，免得导了一半
    issues: list[LocalizedIssue] = []
    rejected = blank = 0
    errors: list[str] = []
    for row in rows:
        try:
            issue = check_row(row)
        except LocalizeError as exc:
            errors.append(str(exc))
            continue
        if issue is None:
            if (row.get("verdict") or "").strip():
                rejected += 1
            else:
                blank += 1
            continue
        issues.append(issue)
    if errors:
        print("对照表有问题，一条都没导：", file=sys.stderr)
        for line in errors:
            print(f"  ✗ {line}", file=sys.stderr)
        return 1
    if not issues:
        print(f"没有 ACCEPT 的行（REJECT {rejected}，没填结论 {blank}）", file=sys.stderr)
        return 1

    engine = create_db_engine()
    factory = create_session_factory(engine)
    done = 0
    with session_scope(factory) as session:
        for issue in issues:
            task = session.execute(
                sa.select(BenchmarkTask).where(BenchmarkTask.task_id == issue.task_id)
            ).scalar_one_or_none()
            if task is None:
                print(f"  ✗ {issue.task_id} 库里没有，整张表不导", file=sys.stderr)
                session.rollback()
                return 1
            if task.validation_state is not TaskValidationState.VALID:
                print(
                    f"  ✗ {issue.task_id} 状态是 {task.validation_state.value}，不是 VALID，"
                    "整张表不导（题面改写只对已验证的题做）",
                    file=sys.stderr,
                )
                session.rollback()
                return 1
            try:
                definition = localize_definition(task.raw_definition or {}, issue)
                spec = _environment_spec(session, definition.environment_id)
            except LocalizeError as exc:
                print(f"  ✗ {exc}", file=sys.stderr)
                session.rollback()
                return 1
            before = task.content_hash
            if args.dry_run:
                print(
                    f"  · {issue.task_id:<30} {before[:12]} → "
                    f"{(definition.content_hash or '')[7:19]}  "
                    f"标题「{definition.issue_title[:30]}」 正文 {len(definition.issue_body)} 字"
                )
                done += 1
                continue
            # uri 列的前缀照抄库里现在的写法（mined:// 或 golden://），不然会被改成别的来源
            scheme = (task.test_patch_uri or "mined://").split("://", 1)[0]
            was = task.validation_state
            upsert_task(
                session, definition, {definition.environment_id: spec}, patch_uri_scheme=scheme
            )
            assert task.validation_state is was, "upsert_task 不该动 validation_state"
            print(
                f"  ✓ {issue.task_id:<30} {before[:12]} → {task.content_hash[:12]}  "
                f"复核 {issue.reviewer}"
            )
            done += 1
        if args.dry_run:
            session.rollback()

    verb = "可导" if args.dry_run else "已导"
    print(f"\n{verb} {done} 道，REJECT {rejected} 道，没填结论 {blank} 道")
    if not args.dry_run:
        print(
            "\n下一步（v1 不动，改写后的题冻成新版本）：\n"
            "  make dataset-stage DATASET=benchmark-dev SLUG=benchmark-cn-v1\n"
            "  make dataset-gate SLUG=benchmark-cn-v1 && make worker\n"
            "  make dataset-publish SLUG=benchmark-cn-v1"
        )
    return 0


# ══════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli.localize", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    draft = sub.add_parser("draft", help="导出对照表：VALID 的自建题，zh 两列留空")
    draft.add_argument("--dataset-id", default=DEFAULT_DATASET_ID)
    draft.add_argument("--task", action="append", help="只导这几道（task_id，可重复）")
    draft.add_argument("--out", help="写到哪，默认 datasets/benchmark-dev/localize-<日期>.csv")
    draft.set_defaults(func=cmd_draft)

    imp = sub.add_parser("import", help="把复核通过（verdict=ACCEPT）的中文题面导回库")
    imp.add_argument("file", help="填好 zh_* / reviewer / verdict 的对照表")
    imp.add_argument("--dry-run", action="store_true", help="只查表、算哈希，不写库")
    imp.set_defaults(func=cmd_import)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
