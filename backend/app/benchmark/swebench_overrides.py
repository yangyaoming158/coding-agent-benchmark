"""官方题的逐题覆盖：剔掉执行器选不中的 P2P 用例（E1-T7，2026-09-16 晚抽到 75 道时加的）。

## 为什么要有这份清单

`swebench_import.clean_test_ids` 用两条纯字符串规则剔掉官方名单里不是 nodeid 的东西
（`[100%]`、被空白切断的 id）。抽到 75 道后又碰到三种**看着像 nodeid、但在我们的平台上
永远选不中**的 id，一条规则都认不出来，只能逐条列：

- 参数里带 `::`（`test_valid_idents[:::]`）—— pytest 5.4 的命令行按 `::` 切 nodeid；
- pytester 子会话里的 id（`test_capsysbinary.py::test_hello`）—— 顶层根本没这个文件，
  是官方日志解析器把子会话的输出串进来了；
- 参数里带绝对路径又被截过（`test_stdin[/mymodule.py]`）—— 在任何环境里都对不上真实 id。

执行器按 C-17 把 F2P ∪ P2P 逐条写在 pytest 命令行上，**一条选不中 pytest 就整场退出（exit 4）**，
所以这些题不剔就一条用例都跑不了。官方 harness 是整文件跑再解析日志，碰不到这个。

## 规矩

- 清单进仓库（`datasets/swebench/p2p-overrides.json`），每条带理由和日期，review 看得见；
- **只许剔 P2P，不许碰 F2P**：F2P 是"修好了"的定义，动它就是另一道题；
- 要剔的 id 必须真的在官方 P2P 里，写错一个字直接报错，不能静默不生效；
- 被剔过的题打 `swebench-p2p-override` 标签，报告里分得开。

改了清单要重跑 `cli.swebench import` 和 `cli.validate run --task`，题目定义变了必须重验（§8.6）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from app.benchmark.swebench_import import VerifiedInstance

#: 被剔过 P2P 的题打这个标签。
P2P_OVERRIDE_TAG = "swebench-p2p-override"


class OverrideError(ValueError):
    """清单写错了：题号对不上、id 不在官方 P2P 里、想动 F2P……宁可报错也不要静默不生效。"""


@dataclass(frozen=True, slots=True)
class P2POverride:
    """一道题的覆盖：剔掉哪些 P2P、为什么、什么时候定的。"""

    instance_id: str
    drop_pass_to_pass: tuple[str, ...]
    reason: str
    decided_on: str


def load_overrides(path: Path) -> dict[str, P2POverride]:
    """读清单。文件不存在就是没有覆盖（空字典），存在就每条都必须写全。"""
    if not path.exists():
        return {}
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    overrides: dict[str, P2POverride] = {}
    for instance_id, entry in data.items():
        if instance_id.startswith("_"):  # `_comment` 之类的说明字段
            continue
        if not isinstance(entry, Mapping):
            raise OverrideError(f"{instance_id}：条目不是对象")
        drops = entry.get("drop_pass_to_pass")
        reason = str(entry.get("reason") or "").strip()
        decided_on = str(entry.get("decided_on") or "").strip()
        if not isinstance(drops, list) or not drops or not all(isinstance(d, str) for d in drops):
            raise OverrideError(f"{instance_id}：`drop_pass_to_pass` 必须是非空的字符串列表")
        if not reason or not decided_on:
            raise OverrideError(f"{instance_id}：`reason` 和 `decided_on` 都要写")
        overrides[instance_id] = P2POverride(
            instance_id=instance_id,
            drop_pass_to_pass=tuple(drops),
            reason=reason,
            decided_on=decided_on,
        )
    return overrides


def apply_override(instance: VerifiedInstance, override: P2POverride) -> VerifiedInstance:
    """按清单剔掉 P2P，返回新的实例；F2P 一个字不动。"""
    if override.instance_id != instance.instance_id:
        raise OverrideError(f"覆盖 {override.instance_id} 用到了 {instance.instance_id} 上")
    forbidden = [d for d in override.drop_pass_to_pass if d in instance.FAIL_TO_PASS]
    if forbidden:
        raise OverrideError(f"{instance.instance_id}：不许剔 F2P：{forbidden}")
    missing = [d for d in override.drop_pass_to_pass if d not in instance.PASS_TO_PASS]
    if missing:
        raise OverrideError(
            f"{instance.instance_id}：这些 id 不在官方 P2P 里，清单写错了：{missing}"
        )
    drops = set(override.drop_pass_to_pass)
    kept = tuple(t for t in instance.PASS_TO_PASS if t not in drops)
    return replace(instance, PASS_TO_PASS=kept)


__all__ = ["P2P_OVERRIDE_TAG", "OverrideError", "P2POverride", "apply_override", "load_overrides"]
