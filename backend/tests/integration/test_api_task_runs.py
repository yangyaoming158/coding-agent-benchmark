"""单题执行接口（E7-T0）：详情、逐条用例、制品下载。

这三个端点是 §16.2 那条核心用户旅程的终点 ——
"3 次点击到达某个 Agent 在某道题上为什么失败"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.domain.enums import (
    AgentOutcome,
    ArtifactBackend,
    ArtifactKind,
    ArtifactOwnerType,
    AttributionStage,
    AttributionStatus,
    CostSource,
    FailureCategory,
    InfraOutcome,
    LifecycleStatus,
    PatchKind,
    TestRole,
    TestStatus,
)
from app.evaluation.orchestrator import create_runs
from app.infrastructure.models.artifact import Artifact
from app.infrastructure.models.attribution import FailureAttribution
from app.infrastructure.models.evaluation import EvaluationTaskRun, PatchArtifact, TestResult
from tests.integration.factories import provenance_for, seed_minimal, wipe

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def clean(session: Session) -> None:
    wipe(session)


@pytest.fixture
def finished_task_run(session: Session) -> EvaluationTaskRun:
    """一次跑完的执行：有判定结论、有用例结果、有补丁统计。"""
    seeded = seed_minimal(session, tasks=1)
    run = create_runs(
        session, name="n", task_ids=list(seeded.task_ids), provenance=provenance_for(seeded)
    )[0]
    task_run = EvaluationTaskRun(
        evaluation_run_id=run.id,
        benchmark_task_id=seeded.task_ids[0],
        lifecycle_status=LifecycleStatus.COMPLETED,
        infra_outcome=InfraOutcome.SUCCESS,
        agent_outcome=AgentOutcome.UNRESOLVED,
        agent_started_at=run.created_at,
        is_canonical=True,
        f2p_passed=1,
        f2p_total=2,
        p2p_passed=3,
        p2p_total=3,
        cost_usd=None,
        cost_source=CostSource.UNAVAILABLE,
        tokens_total=123456,
        raw_patch_empty=False,
        protected_path_edit_attempted=True,
        filtered_change_reasons=[{"path": "tests/test_a.py", "reason": "protected_path"}],
    )
    session.add(task_run)
    session.flush()
    session.add_all(
        [
            TestResult(
                evaluation_task_run_id=task_run.id,
                test_id="tests/test_a.py::test_new",
                role=TestRole.F2P,
                status=TestStatus.FAILED,
                message_excerpt="AssertionError",
            ),
            TestResult(
                evaluation_task_run_id=task_run.id,
                test_id="tests/test_b.py::test_old",
                role=TestRole.P2P,
                status=TestStatus.PASSED,
            ),
            PatchArtifact(
                evaluation_task_run_id=task_run.id,
                kind=PatchKind.AGENT_NORMALIZED,
                uri="local://runs/1/patch.diff",
                sha256="c" * 64,
                size_bytes=512,
                files_changed=2,
                lines_added=10,
                lines_deleted=3,
                is_empty=False,
            ),
        ]
    )
    session.commit()
    return task_run


def test_task_run_detail_keeps_the_three_status_fields_separate(
    client: TestClient, finished_task_run: EvaluationTaskRun
) -> None:
    """三个字段原样透出，既不合并也不做中文映射（协议 C-03/C-04/C-05/C-06，AC-10）。

    合并了解决率就不可信 —— "AI 没修好"和"我们的 Docker 崩了"在报告里
    是完全相反的结论。
    """
    body = client.get(f"/api/task-runs/{finished_task_run.id}").json()

    assert body["lifecycle_status"] == "COMPLETED"
    assert body["infra_outcome"] == "SUCCESS"
    assert body["agent_outcome"] == "UNRESOLVED"
    assert body["f2p_passed"] == 1
    assert body["f2p_total"] == 2


def test_task_run_detail_without_attribution_says_so_without_hiding(
    client: TestClient, finished_task_run: EvaluationTaskRun
) -> None:
    """没有归因行时 `failure_attribution` 是 null，而且 `attribution_withheld` 是 false。

    两个字段要分开：null 有两种意思（修好了 / 还没结论），"被藏起来"是第三种，
    混在一个 null 里前端就没法告诉人"现在是盲检期间"。
    """
    body = client.get(f"/api/task-runs/{finished_task_run.id}").json()

    assert body["failure_attribution"] is None
    assert body["attribution_withheld"] is False


def test_task_run_detail_carries_the_attribution_but_not_the_raw_votes(
    client: TestClient, session: Session, finished_task_run: EvaluationTaskRun
) -> None:
    """归因随详情走：类别、置信度、证据、理由都在，三票原始回答不透出。

    规则层的 `confidence` 是 None 而不是 0 —— 规则是确定性判定，"没有置信度"
    和"置信度为零"是两回事，前端要按 None 显示"规则判定"。
    """
    session.add(
        FailureAttribution(
            evaluation_task_run_id=finished_task_run.id,
            stage=AttributionStage.LLM,
            category=FailureCategory.F4_INCORRECT_LOGIC,
            secondary_category=FailureCategory.F3_INCOMPLETE_FIX,
            confidence=0.875,
            judge_model="fake/judge",
            prompt_hash="a" * 64,
            evidence={
                "citations": [{"source": "patch", "quote": "return x - 1"}],
                "vote_categories": ["F4_INCORRECT_LOGIC"],
            },
            reasoning_zh="改对了位置，但边界条件仍然错。",
            raw_response={"votes": [{"vote_index": 0, "content": "{...}"}]},
            status=AttributionStatus.OK,
        )
    )
    session.commit()

    body = client.get(f"/api/task-runs/{finished_task_run.id}").json()

    attribution = body["failure_attribution"]
    assert attribution["stage"] == "LLM"
    assert attribution["category"] == "F4_INCORRECT_LOGIC"
    assert attribution["secondary_category"] == "F3_INCOMPLETE_FIX"
    assert attribution["confidence"] == "0.875"
    assert attribution["status"] == "OK"
    assert attribution["judge_model"] == "fake/judge"
    assert attribution["evidence"]["citations"] == [{"source": "patch", "quote": "return x - 1"}]
    assert attribution["reasoning_zh"] == "改对了位置，但边界条件仍然错。"
    assert "raw_response" not in attribution
    assert body["attribution_withheld"] is False

    # 判定字段一个没动：归因只解释原因，不回写结论（协议 C-40）
    assert body["agent_outcome"] == "UNRESOLVED"


def test_blind_review_switch_withholds_the_attribution_from_the_open_endpoint(
    client: TestClient,
    session: Session,
    finished_task_run: EvaluationTaskRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`BENCH_BLIND_REVIEW=true` 时，开放读接口的 JSON 里没有归因内容，只有一个"被藏起来"的标记。

    E6-T3 AC 4 的"不能只靠前端隐藏"对这一页同样成立：盲检队列里写着 task_run_id，
    标注的人在地址栏敲一下就到这儿了。藏要藏在后端，而且要说明是藏的，不是没有。
    """
    from app.infrastructure.config import reset_settings_cache

    session.add(
        FailureAttribution(
            evaluation_task_run_id=finished_task_run.id,
            stage=AttributionStage.RULE,
            category=FailureCategory.F6_REGRESSION,
            evidence={"rule": "regression", "facts": {"p2p": "2/3"}},
            status=AttributionStatus.OK,
        )
    )
    session.commit()

    monkeypatch.setenv("BENCH_BLIND_REVIEW", "true")
    reset_settings_cache()
    try:
        withheld = client.get(f"/api/task-runs/{finished_task_run.id}").json()
    finally:
        monkeypatch.delenv("BENCH_BLIND_REVIEW")
        reset_settings_cache()

    assert withheld["failure_attribution"] is None
    assert withheld["attribution_withheld"] is True
    assert "F6_REGRESSION" not in str(withheld)

    # 开关一关，同一条记录立刻可见 —— 证明上面那个 None 是藏的，不是丢了
    visible = client.get(f"/api/task-runs/{finished_task_run.id}").json()
    assert visible["failure_attribution"]["category"] == "F6_REGRESSION"
    assert visible["failure_attribution"]["confidence"] is None
    assert visible["attribution_withheld"] is False


def test_task_run_detail_carries_the_diagnostic_fields(
    client: TestClient, finished_task_run: EvaluationTaskRun
) -> None:
    """协议 C-08b 的三个诊断字段要透出来。

    `EMPTY_PATCH` 本身有歧义："AI 什么都没做"和"改的全是受保护文件、
    被平台丢光了"结果都是空补丁。没有这三个字段，失败分析会把两件事混为一谈。
    """
    body = client.get(f"/api/task-runs/{finished_task_run.id}").json()

    assert body["raw_patch_empty"] is False
    assert body["protected_path_edit_attempted"] is True
    assert body["filtered_change_reasons"] == [
        {"path": "tests/test_a.py", "reason": "protected_path"}
    ]


def test_cost_source_unavailable_shows_null_cost_not_zero(
    client: TestClient, finished_task_run: EvaluationTaskRun
) -> None:
    """报不出成本时金额是 null 且来源是 unavailable（协议纪律 3）。

    显示成 0 就成了"这次不花钱"，而实际花掉的钱一分不少。
    """
    body = client.get(f"/api/task-runs/{finished_task_run.id}").json()

    assert body["cost_usd"] is None
    assert body["cost_source"] == "unavailable"


def test_patch_summary_has_stats_but_no_diff_text(
    client: TestClient, finished_task_run: EvaluationTaskRun
) -> None:
    """补丁在详情里只给统计，正文走制品端点（AC-6）。"""
    patch = client.get(f"/api/task-runs/{finished_task_run.id}").json()["patches"][0]

    assert patch["kind"] == "AGENT_NORMALIZED"
    assert patch["files_changed"] == 2
    assert "diff" not in patch
    assert "content" not in patch


def test_tests_endpoint_lists_cases_with_role_and_status(
    client: TestClient, finished_task_run: EvaluationTaskRun
) -> None:
    body = client.get(f"/api/task-runs/{finished_task_run.id}/tests").json()

    assert body["total"] == 2
    assert {(row["role"], row["status"]) for row in body["items"]} == {
        ("F2P", "FAILED"),
        ("P2P", "PASSED"),
    }


def test_tests_endpoint_can_filter_by_role(
    client: TestClient, finished_task_run: EvaluationTaskRun
) -> None:
    body = client.get(f"/api/task-runs/{finished_task_run.id}/tests", params={"role": "F2P"}).json()

    assert body["total"] == 1
    assert body["items"][0]["test_id"] == "tests/test_a.py::test_new"


def test_tests_of_an_unknown_task_run_is_404(client: TestClient) -> None:
    response = client.get("/api/task-runs/987654/tests")

    assert response.status_code == 404
    assert response.json()["code"] == "TASK_RUN_NOT_FOUND"


# ── 制品 ────────────────────────────────────────────────────


def test_artifact_is_streamed_not_embedded_in_json(
    client: TestClient,
    session: Session,
    finished_task_run: EvaluationTaskRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本地存储没有签名 URL，所以是流式转发；内容不进 JSON（AC-6）。

    一次评测的 Agent stdout 能到几百 MB，塞进 JSON 会把前端和后端内存一起打挂。
    """
    from app.infrastructure.config import reset_settings_cache
    from app.storage import create_artifact_store

    monkeypatch.setenv("ARTIFACT_LOCAL_ROOT", str(tmp_path))
    reset_settings_cache()
    try:
        payload = b"agent said a lot of things\n" * 100
        ref = create_artifact_store().put(
            f"runs/1/task-runs/{finished_task_run.id}/agent_stdout.log",
            payload,
            content_type="text/plain",
        )
        session.add(
            Artifact(
                owner_type=ArtifactOwnerType.TASK_RUN,
                owner_id=finished_task_run.id,
                kind=ArtifactKind.AGENT_STDOUT,
                uri=ref.uri,
                backend=ArtifactBackend.LOCAL,
                content_type=ref.content_type,
                size_bytes=ref.size_bytes,
                sha256=ref.sha256,
                compressed=ref.compressed,
            )
        )
        session.commit()

        listed = client.get(f"/api/task-runs/{finished_task_run.id}").json()["artifacts"]
        assert [a["kind"] for a in listed] == ["AGENT_STDOUT"]
        assert "uri" not in listed[0]  # 物理位置不外泄，前端按 kind 拼链接

        response = client.get(f"/api/task-runs/{finished_task_run.id}/artifacts/AGENT_STDOUT")
        assert response.status_code == 200
        # 存的是 gzip，取回来是解压后的原文 —— 压缩由存储层透明处理
        assert response.content == payload
        assert response.headers["content-type"].startswith("text/plain")
        assert response.headers["x-artifact-sha256"] == ref.sha256
    finally:
        reset_settings_cache()


def test_artifact_redirects_when_the_store_can_sign_a_url(
    client: TestClient,
    session: Session,
    finished_task_run: EvaluationTaskRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """存储能给签名 URL 时就 302 过去，字节一个都不经过后端（AC-6）。

    判断依据是 `store.url()` 的返回值，不是配置项 —— E10-T2 接 MinIO 时
    这段代码一行都不用改（ADR-005 那句"业务代码零改动"）。
    """
    session.add(
        Artifact(
            owner_type=ArtifactOwnerType.TASK_RUN,
            owner_id=finished_task_run.id,
            kind=ArtifactKind.TEST_STDOUT,
            uri="minio://runs/1/test_stdout.log.gz",
            backend=ArtifactBackend.MINIO,
            content_type="text/plain",
            size_bytes=10,
            sha256="d" * 64,
            compressed=True,
        )
    )
    session.commit()

    class SigningStore:
        def url(self, _key: str, *, expires_s: int = 3600) -> str:
            return "https://minio.example/signed?token=abc"

    monkeypatch.setattr("app.api.task_runs.create_artifact_store", lambda *_a, **_k: SigningStore())

    response = client.get(
        f"/api/task-runs/{finished_task_run.id}/artifacts/TEST_STDOUT",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "https://minio.example/signed?token=abc"


def test_patch_text_is_served_from_the_patch_table(
    client: TestClient,
    session: Session,
    finished_task_run: EvaluationTaskRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """补丁正文也从这个端点拿，尽管它在 `patch_artifacts` 而不是 `artifacts`。

    §16.2 的 Task Run Detail 页那个 **Patch Viewer 要的就是这段 diff 正文**。
    详情接口只给补丁的统计（AC-6：正文不进 JSON），正文只有这一条路。
    只查 `artifacts` 表的话这里永远 404 —— 而 `ArtifactKind.PATCH` 这个值
    全库没有任何一处往里写（2026-09-12 查过库，7 种 kind 里没有它）。
    """
    from app.infrastructure.config import reset_settings_cache
    from app.storage import create_artifact_store

    monkeypatch.setenv("ARTIFACT_LOCAL_ROOT", str(tmp_path))
    reset_settings_cache()
    try:
        diff = b"--- a/src/x.py\n+++ b/src/x.py\n@@ -1 +1 @@\n-old\n+new\n"
        ref = create_artifact_store().put(
            f"runs/1/task-runs/{finished_task_run.id}/patch.diff",
            diff,
            content_type="text/x-diff",
        )
        patch = session.execute(
            sa.select(PatchArtifact).where(
                PatchArtifact.evaluation_task_run_id == finished_task_run.id
            )
        ).scalar_one()
        patch.uri = ref.uri
        patch.sha256 = ref.sha256
        patch.size_bytes = ref.size_bytes
        session.commit()

        response = client.get(f"/api/task-runs/{finished_task_run.id}/artifacts/AGENT_NORMALIZED")

        assert response.status_code == 200
        assert response.content == diff
        assert response.headers["content-type"].startswith("text/x-diff")
        assert response.headers["x-artifact-sha256"] == ref.sha256
    finally:
        reset_settings_cache()


def test_missing_patch_kind_is_404(
    client: TestClient, finished_task_run: EvaluationTaskRun
) -> None:
    """夹具只建了标准化补丁，问原始补丁要 404，不是 500。"""
    response = client.get(f"/api/task-runs/{finished_task_run.id}/artifacts/AGENT_RAW")

    assert response.status_code == 404
    assert response.json()["code"] == "ARTIFACT_NOT_FOUND"


def test_missing_artifact_kind_is_404(
    client: TestClient, finished_task_run: EvaluationTaskRun
) -> None:
    response = client.get(f"/api/task-runs/{finished_task_run.id}/artifacts/TRAJECTORY")

    assert response.status_code == 404
    assert response.json()["code"] == "ARTIFACT_NOT_FOUND"


def test_artifact_row_without_the_file_is_404_not_an_empty_body(
    client: TestClient,
    session: Session,
    finished_task_run: EvaluationTaskRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """库里有索引行、磁盘上文件没了 —— 要说出来，不能返回一个空文件。

    空日志和"日志被清掉了"是两回事。§18.6 第十节记过一次同源的坑：
    制品目录不跟着清库，拿目录当数据源会数出假的结论。
    """
    from app.infrastructure.config import reset_settings_cache

    monkeypatch.setenv("ARTIFACT_LOCAL_ROOT", str(tmp_path))
    reset_settings_cache()
    try:
        session.add(
            Artifact(
                owner_type=ArtifactOwnerType.TASK_RUN,
                owner_id=finished_task_run.id,
                kind=ArtifactKind.AGENT_STDERR,
                uri="local://runs/1/gone.log.gz",
                backend=ArtifactBackend.LOCAL,
                content_type="text/plain",
                size_bytes=10,
                sha256="e" * 64,
                compressed=True,
            )
        )
        session.commit()

        response = client.get(f"/api/task-runs/{finished_task_run.id}/artifacts/AGENT_STDERR")

        assert response.status_code == 404
        assert response.json()["code"] == "ARTIFACT_FILE_MISSING"
    finally:
        reset_settings_cache()


@pytest.mark.parametrize("kind", ["GOLD", "TEST"])
def test_official_patches_can_never_be_fetched(
    client: TestClient, session: Session, finished_task_run: EvaluationTaskRun, kind: str
) -> None:
    """官方补丁**取不出来**，即使库里真有那一行。

    `PatchKind` 一共四个值，`GOLD` 是官方修复补丁（协议 C-44 禁止它到达被测 AI）、
    `TEST` 是官方测试补丁（C-76）。而这是个**不要 token 的开放读接口**。

    挡法不是在函数里加一句 if，是让端点的 kind 参数根本不接受这两个值 ——
    所以这里是 422（参数非法），不是 403 也不是 404，
    而且 `make gen-api` 生成的前端类型里压根没有这两个选项。
    """
    session.add(
        PatchArtifact(
            evaluation_task_run_id=finished_task_run.id,
            kind=PatchKind(kind),
            uri="local://runs/1/official.diff",
            sha256="f" * 64,
            size_bytes=100,
            files_changed=1,
            lines_added=1,
            lines_deleted=0,
            is_empty=False,
        )
    )
    session.commit()

    response = client.get(f"/api/task-runs/{finished_task_run.id}/artifacts/{kind}")

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_PARAMETER"


def test_unknown_artifact_kind_is_422(
    client: TestClient, finished_task_run: EvaluationTaskRun
) -> None:
    """路径里的 kind 不是合法枚举值时是 422，错误体仍然是 {code, message}。"""
    response = client.get(f"/api/task-runs/{finished_task_run.id}/artifacts/NOPE")

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_PARAMETER"


def test_unknown_task_run_detail_is_404(client: TestClient) -> None:
    response = client.get("/api/task-runs/987654")

    assert response.status_code == 404
    assert response.json() == {
        "code": "TASK_RUN_NOT_FOUND",
        "message": "找不到执行记录 #987654",
    }


def test_unknown_route_also_uses_the_standard_error_shape(client: TestClient) -> None:
    """连路由都没匹配上时也是 {code, message}，不是 FastAPI 默认的 detail。

    前端只写一套错误解析，不该为"接口不存在"再写一套。
    """
    body: dict[str, Any] = client.get("/api/nope").json()

    assert set(body) == {"code", "message"}
    assert body["code"] == "NOT_FOUND"
