"""把 SWE-bench 官方包生成的构建脚本导出成 JSON（E1-T7，本机建镜像用）。

    uv run --with "swebench==3.0.15" --isolated python scripts/export_swebench_specs.py

为什么单独一个脚本、而不是让后端直接 import swebench：`swebench` 拖着 datasets / pyarrow /
pandas 一串重依赖，只为拿几段 shell 脚本不值得进 `pyproject.toml`。脚本一次性跑完，
把 50 道题的三层 Dockerfile 和两段安装脚本落成 `datasets/swebench/build-specs.json`
（去重后只有 20 个环境），`cli.swebench build` 只读这个 JSON。

导出的是**官方原样**，不改一个字：改源、换 clone 方式那些事在 `cli.swebench build` 里做，
这样原文和改动能分开看。文件里记着 swebench 的版本号，将来对不上能查。
"""

from __future__ import annotations

import hashlib
import json
import sys
from importlib.metadata import version
from pathlib import Path

from swebench.harness.test_spec.test_spec import make_test_spec  # type: ignore[import-not-found]

REPO_ROOT = Path(__file__).resolve().parents[1]
ROWS = REPO_ROOT / "var" / "cache" / "swebench" / "verified.jsonl"
SAMPLE = REPO_ROOT / "datasets" / "swebench" / "sample-seed20260915-n50.json"
OUT = REPO_ROOT / "datasets" / "swebench" / "build-specs.json"


def main() -> int:
    rows = {}
    for line in ROWS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row["instance_id"]] = row
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))

    envs: dict[str, dict[str, str]] = {}
    instances: dict[str, dict[str, str]] = {}
    base_dockerfile = None
    for instance_id in sample["chosen"]:
        spec = make_test_spec(rows[instance_id])
        base_dockerfile = spec.base_dockerfile
        envs.setdefault(
            spec.env_image_key,
            {"env_dockerfile": spec.env_dockerfile, "setup_env_script": spec.setup_env_script},
        )
        instances[instance_id] = {
            "env_image_key": spec.env_image_key,
            "instance_dockerfile": spec.instance_dockerfile,
            "install_repo_script": spec.install_repo_script,
            "eval_script": spec.eval_script,
        }

    payload = {
        "swebench_version": version("swebench"),
        "sample": SAMPLE.name,
        "base_dockerfile": base_dockerfile,
        "envs": envs,
        "instances": instances,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    OUT.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    print(f"{len(instances)} 道题，{len(envs)} 个环境，swebench {payload['swebench_version']}")
    print(f"写入 {OUT}（{len(text) / 1024:.0f} KB，sha256 {digest}…）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
