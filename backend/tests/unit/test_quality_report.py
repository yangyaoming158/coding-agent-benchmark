"""数据集质量报告（E8-T5，`cli/quality.py`）：纯函数部分，不碰库。"""

from __future__ import annotations

from app.benchmark.swebench_import import Funnel
from cli.quality import (
    QualityReport,
    SetProfile,
    TaskRow,
    mined_funnel,
    render_markdown,
)


def a_task(
    task_id: str,
    *,
    repo: str = "pallets/click",
    language: str = "en",
    difficulty: str = "medium",
    f2p: int = 2,
    p2p: int = 1000,
    domestic: bool = False,
) -> TaskRow:
    return TaskRow(
        task_id=task_id,
        repo=repo,
        is_domestic=domestic,
        language=language,
        difficulty=difficulty,
        f2p_count=f2p,
        p2p_count=p2p,
        agent_timeout_s=720,
        test_timeout_s=480,
    )


def a_profile(tasks: list[TaskRow], *, slug: str = "benchmark-cn-v1") -> SetProfile:
    return SetProfile(
        slug=slug,
        version="v1",
        status="PUBLISHED",
        source_dataset_id="benchmark-dev",
        published_at="2026-09-17T10:00:00+00:00",
        snapshot_digest="sha256:" + "ab" * 32,
        gate={
            "oracle": {"evaluation_run_id": 139, "resolved_count": 2, "total_tasks": 2},
            "noop": {"evaluation_run_id": 140, "resolved_count": 0, "total_tasks": 2},
        },
        tasks=tasks,
    )


# ── 画像 ─────────────────────────────────────────────────────


def test_profile_counts_come_from_the_snapshot_rows() -> None:
    profile = a_profile(
        [
            a_task("a-1", language="zh", difficulty="easy", f2p=1, p2p=10),
            a_task("a-2", language="mixed", f2p=3, p2p=30, repo="tortoise/tortoise-orm"),
            a_task("a-3", difficulty="hard", f2p=2, p2p=20),
        ]
    )
    assert profile.task_count == 3
    assert profile.by_repo() == {"pallets/click": 2, "tortoise/tortoise-orm": 1}
    assert profile.languages() == {"zh": 1, "mixed": 1, "en": 1}
    assert profile.difficulty() == {"easy": 1, "medium": 1, "hard": 1}
    assert profile.f2p() == {"total": 6, "median": 2.0, "min": 1, "max": 3}
    assert profile.p2p() == {"total": 60, "median": 20.0, "min": 10, "max": 30}
    assert profile.timeouts() == {"agent_timeout_s": {"720": 3}, "test_timeout_s": {"480": 3}}


def test_empty_profile_has_zero_stats() -> None:
    profile = a_profile([])
    assert profile.f2p() == {"total": 0, "median": 0, "min": 0, "max": 0}
    assert profile.to_dict()["task_ids"] == []


# ── 自建题漏斗 ───────────────────────────────────────────────


def payload(
    decision: str | None = "PASS",
    *,
    f2p: bool = True,
    probe: str | None = "OK",
    verdict: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {}
    if decision is not None:
        body["prescreen"] = {"decision": decision}
    body["cleaned"] = {"f2p_candidates": ["tests/t.py::test_x"] if f2p else []}
    if probe is not None:
        body["probe"] = {"state": probe}
    if verdict is not None:
        body["final_review"] = {"verdict": verdict}
    return body


def test_mined_funnel_counts_each_layer_per_repo() -> None:
    """每一层只数上一层活下来的：预筛 REJECT 的不数候选 F2P，抽不出 F2P 的不数探测。"""
    funnels = mined_funnel(
        candidates=[
            ("pallets/click", payload("PASS", verdict="ACCEPT")),
            ("pallets/click", payload("REVIEW", probe="F2P_NOT_FAILING")),
            ("pallets/click", payload("REJECT")),  # 预筛否掉，后面不数
            ("pallets/click", payload("PASS", f2p=False)),  # 抽不出候选 F2P，不进探测
            ("pallets/click", payload(None)),  # 没预筛，记 "—"
            ("Delgan/loguru", payload("PASS", probe=None)),  # 没探过
        ],
        tasks=[("pallets/click", "VALID"), ("pallets/click", "INVALID")],
        in_set=["pallets/click"],
    )
    by_repo = {f.repo: f for f in funnels}
    click = by_repo["pallets/click"]
    assert click.candidates == 5
    assert click.prescreen == {"PASS": 2, "REVIEW": 1, "REJECT": 1, "—": 1}
    assert click.promotable == 3
    assert click.with_f2p_candidate == 2
    assert click.probe == {"OK": 1, "F2P_NOT_FAILING": 1}
    assert click.probe_ok == 1
    assert click.tasks == {"VALID": 1, "INVALID": 1}
    assert click.valid == 1
    assert click.final_review == {"ACCEPT": 1}
    assert click.in_set == 1

    loguru = by_repo["Delgan/loguru"]
    assert loguru.probe == {"未探测": 1}
    assert loguru.in_set == 0
    # 进集多的排前面，其次候选多的
    assert [f.repo for f in funnels] == ["pallets/click", "Delgan/loguru"]


def test_mined_funnel_tolerates_null_payload() -> None:
    (funnel,) = mined_funnel([("x/y", None)], tasks=[], in_set=[])
    assert funnel.candidates == 1
    assert funnel.prescreen == {"—": 1}
    assert funnel.promotable == 0


# ── 渲染 ─────────────────────────────────────────────────────


def a_report(official: Funnel | None = None) -> QualityReport:
    mined = a_profile(
        [
            a_task("pallets__click-1", language="zh"),
            a_task("tortoise__tortoise-orm-1", repo="tortoise/tortoise-orm", language="mixed"),
        ]
    )
    official_profile = SetProfile(
        slug="swebench-verified-subset",
        version="v2",
        status="PUBLISHED",
        source_dataset_id="swebench-verified-subset",
        published_at=None,
        snapshot_digest=None,
        gate=None,
        tasks=[a_task("pytest-dev__pytest-1", repo="pytest-dev/pytest", difficulty="easy")],
    )
    return QualityReport(
        generated_at="2026-09-17T12:00:00+00:00",
        profiles=[mined, official_profile],
        unpublished_chinese={"golden-v1": 4},
        mined=mined_funnel(
            [("pallets/click", payload("PASS", verdict="ACCEPT"))],
            [("pallets/click", "VALID")],
            ["pallets/click", "tortoise/tortoise-orm"],
        ),
        mined_source="benchmark-dev",
        official=official,
    )


def test_totals_add_up_across_sets() -> None:
    totals = a_report().totals()
    assert totals["task_count"] == 3
    assert totals["chinese_count"] == 2
    assert totals["languages"] == {"zh": 1, "mixed": 1, "en": 1}
    assert totals["difficulty"] == {"medium": 2, "easy": 1}


def test_markdown_names_every_section_and_the_unpublished_chinese_tasks() -> None:
    text = render_markdown(a_report())
    for heading in (
        "## 一、总览",
        "## 二、来源构成",
        "## 三、语言分布",
        "## 四、难度分布",
        "## 五、规模",
        "## 六、自建题漏斗",
        "## 七、官方题漏斗",
    ):
        assert heading in text, heading
    assert "| **合计** | **3** |" in text
    assert "Oracle #139 2/2 · Noop #140 0/2" in text
    assert "未发布（没过门禁）" in text  # 官方那份没带 gate
    # Golden 那 4 道中文题不算可评测的题，但要交代
    assert "`golden-v1` 4 道" in text
    assert "| **合计** | 1 | 1 | 1 | **2**（67%） |" in text
    # 没有官方原料时如实说
    assert "本机没有官方数据的原料文件" in text


def test_markdown_embeds_the_official_funnel_when_present() -> None:
    funnel = Funnel(official_total=500, sampled=75, imported=74)
    funnel.validation["VALID"] = 59
    text = render_markdown(a_report(official=funnel))
    assert "| 官方题数 | 500 |" in text
    assert "| **VALID** | **59** |" in text
    assert "本机没有官方数据的原料文件" not in text


def test_to_dict_is_json_shaped() -> None:
    import json

    data = a_report().to_dict()
    json.dumps(data)  # 不能有 Counter / dataclass 之类序列化不了的东西
    assert data["totals"]["task_count"] == 3
    assert data["mined_funnel"]["source_dataset_id"] == "benchmark-dev"
    assert data["mined_funnel"]["repos"][0]["repo"] == "pallets/click"
    assert data["official_funnel"] is None
