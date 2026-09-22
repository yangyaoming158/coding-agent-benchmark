"""生成过的报告列表与下载（E7-T9）。"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.domain.enums import AgentKind, ReportFormat
from app.infrastructure.config import reset_settings_cache
from app.report.aggregate import build_report
from app.report.render import render_html
from app.report.service import GeneratedReport, persist_report
from app.storage import create_artifact_store
from tests.integration.factories import wipe
from tests.integration.test_api_leaderboard import World

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def clean(session: Session) -> None:
    wipe(session)


def _persist(
    session: Session, run_ids: list[int], *, tmp_path: Path, generated_at: datetime
) -> GeneratedReport:
    os.environ["ARTIFACT_LOCAL_ROOT"] = str(tmp_path)
    reset_settings_cache()
    report = build_report(session, run_ids, generated_at=generated_at)
    generated = persist_report(session, create_artifact_store(), report)
    session.commit()
    return generated


def test_list_groups_three_formats_into_one_batch_with_dataset_label(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    world = World(session)
    config_id = world.contestant("aider", "aider@deepseek-flash", kind=AgentKind.CLI)
    run = world.run(config_id, resolved=2)

    try:
        _persist(
            session, [run.id], tmp_path=tmp_path, generated_at=datetime(2026, 9, 20, tzinfo=UTC)
        )

        response = client.get("/api/reports")
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["total"] == 1
        (batch,) = body["items"]
        assert batch["scope"] == "SINGLE_RUN"
        assert batch["run_ids"] == [run.id]
        assert batch["dataset_labels"] == ["benchmark-dev@v1 · aider@deepseek-flash"]
        formats = {item["format"] for item in batch["formats"]}
        assert formats == {"HTML", "MARKDOWN", "JSON"}
        # 三种格式对应三个不同的 report_record id，下载端点靠它定位制品
        assert len({item["report_record_id"] for item in batch["formats"]}) == 3
    finally:
        reset_settings_cache()


def test_two_generations_are_two_batches_newest_first(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    world = World(session)
    config_id = world.contestant("aider", "aider@deepseek-flash", kind=AgentKind.CLI)
    older_run = world.run(config_id, resolved=1)
    newer_run = world.run(config_id, resolved=3)

    try:
        _persist(
            session,
            [older_run.id],
            tmp_path=tmp_path,
            generated_at=datetime(2026, 9, 19, tzinfo=UTC),
        )
        _persist(
            session,
            [newer_run.id],
            tmp_path=tmp_path,
            generated_at=datetime(2026, 9, 21, tzinfo=UTC),
        )

        body = client.get("/api/reports").json()
        assert body["total"] == 2
        assert [item["run_ids"] for item in body["items"]] == [[newer_run.id], [older_run.id]]
    finally:
        reset_settings_cache()


def test_pagination_limits_batches_not_records(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    world = World(session)
    config_id = world.contestant("aider", "aider@deepseek-flash", kind=AgentKind.CLI)
    try:
        for day in range(1, 4):
            run = world.run(config_id, resolved=1)
            _persist(
                session,
                [run.id],
                tmp_path=tmp_path,
                generated_at=datetime(2026, 9, day, tzinfo=UTC),
            )

        first_page = client.get("/api/reports", params={"limit": 2, "offset": 0}).json()
        assert first_page["total"] == 3
        assert len(first_page["items"]) == 2

        second_page = client.get("/api/reports", params={"limit": 2, "offset": 2}).json()
        assert len(second_page["items"]) == 1
    finally:
        reset_settings_cache()


def test_download_streams_the_rendered_report_body(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    world = World(session)
    config_id = world.contestant("aider", "aider@deepseek-flash", kind=AgentKind.CLI)
    run = world.run(config_id, resolved=1)

    try:
        generated = _persist(
            session, [run.id], tmp_path=tmp_path, generated_at=datetime(2026, 9, 20, tzinfo=UTC)
        )
        html_record_id = generated.record_ids[ReportFormat.HTML]

        response = client.get(f"/api/reports/{html_record_id}/download")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.content.decode("utf-8") == render_html(generated.data)
    finally:
        reset_settings_cache()


def test_download_unknown_id_is_404(client: TestClient, session: Session) -> None:
    response = client.get("/api/reports/999999/download")

    assert response.status_code == 404
    assert response.json()["code"] == "REPORT_NOT_FOUND"


def test_download_redirects_when_the_store_can_sign_a_url(
    client: TestClient,
    session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(session)
    config_id = world.contestant("aider", "aider@deepseek-flash", kind=AgentKind.CLI)
    run = world.run(config_id, resolved=1)

    try:
        generated = _persist(
            session, [run.id], tmp_path=tmp_path, generated_at=datetime(2026, 9, 20, tzinfo=UTC)
        )
        html_record_id = generated.record_ids[ReportFormat.HTML]

        class SigningStore:
            def url(self, _key: str, *, expires_s: int = 3600) -> str:
                return "https://minio.example/signed?token=abc"

        monkeypatch.setattr(
            "app.api.reports.create_artifact_store", lambda *_a, **_k: SigningStore()
        )

        response = client.get(f"/api/reports/{html_record_id}/download", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers["location"] == "https://minio.example/signed?token=abc"
    finally:
        reset_settings_cache()
