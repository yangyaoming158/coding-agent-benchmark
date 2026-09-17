"""官方题的逐题 P2P 覆盖（E1-T7，`app.benchmark.swebench_overrides`）。

清单里的每一条都是一个"执行器按名字选不中的 id"，规矩是：只许剔 P2P、id 必须真在官方
名单里、被剔过的题打标签。最后一条测试拿仓库里那份真实清单对着官方数据核一遍。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.benchmark.swebench_import import (
    build_task,
    load_instances,
    official_environment,
    parse_instance,
)
from app.benchmark.swebench_overrides import (
    P2P_OVERRIDE_TAG,
    OverrideError,
    P2POverride,
    apply_override,
    load_overrides,
)
from tests.unit.test_swebench_import import row

REPO_ROOT = Path(__file__).resolve().parents[3]
OVERRIDES_FILE = REPO_ROOT / "datasets" / "swebench" / "p2p-overrides.json"
ROWS_FILE = REPO_ROOT / "var" / "cache" / "swebench" / "verified.jsonl"


def _override(**kw: object) -> P2POverride:
    data: dict[str, object] = {
        "instance_id": "pallets__flask-4045",
        "drop_pass_to_pass": ("tests/test_basic.py::test_old",),
        "reason": "测试用",
        "decided_on": "2026-09-16",
    }
    data.update(kw)
    return P2POverride(**data)  # type: ignore[arg-type]


def test_apply_override_drops_only_the_listed_p2p() -> None:
    instance = parse_instance(
        row(PASS_TO_PASS=["tests/test_basic.py::test_old", "tests/test_basic.py::test_keep"])
    )
    after = apply_override(instance, _override())
    assert after.PASS_TO_PASS == ("tests/test_basic.py::test_keep",)
    assert after.FAIL_TO_PASS == instance.FAIL_TO_PASS  # F2P 一个字不动
    assert instance.PASS_TO_PASS[0] == "tests/test_basic.py::test_old"  # 原实例没被改


def test_apply_override_refuses_f2p_unknown_ids_and_wrong_instance() -> None:
    instance = parse_instance(row())
    with pytest.raises(OverrideError, match="不许剔 F2P"):
        apply_override(instance, _override(drop_pass_to_pass=("tests/test_basic.py::test_new",)))
    with pytest.raises(OverrideError, match="不在官方 P2P 里"):
        apply_override(instance, _override(drop_pass_to_pass=("tests/test_basic.py::nope",)))
    with pytest.raises(OverrideError, match="用到了"):
        apply_override(instance, _override(instance_id="pallets__flask-1"))


def test_build_task_tags_overridden_tasks() -> None:
    instance = parse_instance(row())
    env = official_environment(instance, python_version="3.11.9")
    plain = build_task(instance, env)
    tagged = build_task(apply_override(instance, _override()), env, extra_tags=(P2P_OVERRIDE_TAG,))
    assert P2P_OVERRIDE_TAG in tagged.tags and P2P_OVERRIDE_TAG not in plain.tags
    assert tagged.pass_to_pass == [] and plain.pass_to_pass == ["tests/test_basic.py::test_old"]
    assert tagged.content_hash != plain.content_hash  # 题目定义变了，必须重验


def test_load_overrides_validates_every_entry(tmp_path: Path) -> None:
    missing = tmp_path / "none.json"
    assert load_overrides(missing) == {}

    good = tmp_path / "good.json"
    good.write_text(
        json.dumps(
            {
                "_comment": "说明字段跳过",
                "a__b-1": {"drop_pass_to_pass": ["t.py::x"], "reason": "r", "decided_on": "d"},
            }
        ),
        encoding="utf-8",
    )
    loaded = load_overrides(good)
    assert list(loaded) == ["a__b-1"] and loaded["a__b-1"].drop_pass_to_pass == ("t.py::x",)

    for broken in (
        {"a__b-1": {"drop_pass_to_pass": [], "reason": "r", "decided_on": "d"}},
        {"a__b-1": {"drop_pass_to_pass": ["t.py::x"], "reason": "", "decided_on": "d"}},
        {"a__b-1": {"drop_pass_to_pass": ["t.py::x"], "reason": "r"}},
        {"a__b-1": ["t.py::x"]},
    ):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(broken), encoding="utf-8")
        with pytest.raises(OverrideError):
            load_overrides(bad)


@pytest.mark.skipif(not ROWS_FILE.exists(), reason="本机没有官方数据缓存")
def test_repo_override_list_matches_official_rows() -> None:
    """仓库里那份清单：每道题都在官方数据里，每条 id 都真在它的 P2P 里、不在 F2P 里。"""
    overrides = load_overrides(OVERRIDES_FILE)
    assert overrides, "清单是空的？"
    by_id = {i.instance_id: i for i in load_instances(ROWS_FILE)}
    for instance_id, override in overrides.items():
        after = apply_override(by_id[instance_id], override)  # 写错一个字这里就抛
        assert len(after.PASS_TO_PASS) == len(by_id[instance_id].PASS_TO_PASS) - len(
            override.drop_pass_to_pass
        )
