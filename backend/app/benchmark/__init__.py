"""题库领域：题目 Schema、验证流水线、GitHub 挖掘、数据集版本。

依赖规则：可依赖 sandbox / judge / storage / infrastructure / domain。
"""

from app.benchmark.hashing import canonical_json, compute_content_hash, to_bare_hex
from app.benchmark.patch_paths import derive_patch_paths
from app.benchmark.schema import P2PSampling, TaskDefinition, TaskValidation

__all__ = [
    "P2PSampling",
    "TaskDefinition",
    "TaskValidation",
    "canonical_json",
    "compute_content_hash",
    "derive_patch_paths",
    "to_bare_hex",
]
