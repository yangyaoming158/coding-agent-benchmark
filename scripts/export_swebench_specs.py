"""把 SWE-bench 官方包生成的构建脚本导出成 JSON（E1-T7，本机建镜像用）。

    uv run --with "swebench==3.0.15" --isolated python scripts/export_swebench_specs.py \
        [--seed 20260915] [--n 75]

为什么单独一个脚本、而不是让后端直接 import swebench：`swebench` 拖着 datasets / pyarrow /
pandas 一串重依赖，只为拿几段 shell 脚本不值得进 `pyproject.toml`。脚本一次性跑完，
把抽中的题的三层 Dockerfile 和两段安装脚本落成 `datasets/swebench/build-specs.json`，
`cli.swebench build` 只读这个 JSON。环境数比版本数少：官方的 env key 是安装脚本的哈希，
脚本一样的版本共用一层（2026-09-16 抽 50 道时 20 个环境，抽到 75 道后 23 个）。

`--n` 要和 `cli.swebench sample --n` 给的一样，读 `datasets/swebench/sample-seed<seed>-n<n>.json`。
层内顺序是固定的，所以 75 的名单前 50 道和 50 的名单逐字相同，只是多出 25 道。

导出的是**官方原样**，不改一个字：改源、换 clone 方式那些事在 `cli.swebench build` 里做，
这样原文和改动能分开看。文件里记着 swebench 的版本号，将来对不上能查。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from importlib.metadata import version
from pathlib import Path

from swebench.harness.test_spec.test_spec import make_test_spec  # type: ignore[import-not-found]

REPO_ROOT = Path(__file__).resolve().parents[1]
ROWS = REPO_ROOT / "var" / "cache" / "swebench" / "verified.jsonl"
SAMPLE_DIR = REPO_ROOT / "datasets" / "swebench"
OUT = SAMPLE_DIR / "build-specs.json"
#: 和 `app.benchmark.swebench_import` 的 DEFAULT_SEED / DEFAULT_SAMPLE_SIZE 一致
#: （这里不 import 后端，脚本是在隔离环境里跑的）。
DEFAULT_SEED = 20260915
DEFAULT_N = 75


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="抽样种子")
    parser.add_argument("--n", type=int, default=DEFAULT_N, help=f"抽了几道，默认 {DEFAULT_N}")
    args = parser.parse_args()
    sample_file = SAMPLE_DIR / f"sample-seed{args.seed}-n{args.n}.json"
    if not sample_file.exists():
        print(f"没有 {sample_file}，先跑 `cli.swebench sample --seed {args.seed} --n {args.n}`")
        return 1

    rows = {}
    for line in ROWS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row["instance_id"]] = row
    sample = json.loads(sample_file.read_text(encoding="utf-8"))

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
        "sample": sample_file.name,
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
