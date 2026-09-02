#!/usr/bin/env python3
"""提交前扫描密钥。用纯 Python 实现，不依赖外部二进制，离线可用。

对应 AGENTS.md 第 7 节：这个项目要接好几个大模型服务商，密钥泄漏风险高。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 各服务商的密钥格式 + 通用私钥块
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OpenAI/DeepSeek 风格密钥", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("Anthropic 密钥", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("GitHub token", re.compile(r"\b(ghp|gho|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("GitHub PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("私钥文件", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("阿里云 AccessKey", re.compile(r"\bLTAI[A-Za-z0-9]{12,}")),
]

# 这些文件本来就要写密钥的格式示例，跳过
SKIP = {"scripts/check_secrets.py", ".env.example"}


def scan(path: Path) -> list[str]:
    if str(path).replace("\\", "/") in SKIP:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []  # 二进制或读不了的文件跳过
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, pat in PATTERNS:
            if pat.search(line):
                hits.append(f"{path}:{lineno} 疑似{name}")
    return hits


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv[1:]] or list(Path(".").rglob("*"))
    all_hits = [h for f in files if f.is_file() for h in scan(f)]
    if all_hits:
        print("发现疑似密钥，已阻止提交：\n", file=sys.stderr)
        for h in all_hits:
            print(f"  {h}", file=sys.stderr)
        print(
            "\n密钥应放在 .env（已被 .gitignore 排除），并 chmod 600。\n"
            "如果这是误报，把文件加进本脚本的 SKIP 集合。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
