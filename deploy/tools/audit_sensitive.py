#!/usr/bin/env python3
"""公开仓库敏感信息扫描（Issue #57）。

用途：在仓库公开或交付审计前，对“当前跟踪文件”和“历史提交内容”做可复现的
模式扫描。结果只输出文件名、行号与命中类型，**不输出命中原文**，避免扫描
工具本身泄露凭据。发现疑似真实凭据时应立即轮换，并按需评估历史重写。

用法：
    python3 deploy/tools/audit_sensitive.py            # 扫描当前工作树
    python3 deploy/tools/audit_sensitive.py --history  # 额外扫描 git 历史新增行
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# 只覆盖可被静态识别的凭据形态；业务标识符（如变量名 token）不算命中。
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("wechat-id", re.compile(r"\bwxid_[A-Za-z0-9]{8,}\b")),
    ("cn-mobile", re.compile(r"(?<![0-9A-Za-z])1[3-9]\d{9}(?![0-9A-Za-z])")),
    # 赋值为明显占位符的白名单在下面过滤，避免测试夹具误报。
    ("password-assignment", re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*['\"]([^'\"]{8,})['\"]")),
]

# 文档、示例与测试中的显式占位符不视为泄露。
PLACEHOLDER = re.compile(
    r"(?i)(example|sample|dummy|placeholder|changeme|change-me|your[-_]|<[^>]+>|\*{3,}|x{6,}|test|fake|redacted)"
)
TEXT_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".md", ".yml", ".yaml",
    ".ps1", ".sh", ".css", ".html", ".ini", ".toml", ".cfg", ".iss", ".txt",
}
SKIP_DIRS = {".git", "node_modules", "__pycache__", "dist", "build", ".venv", "venv", "storybook-static"}


def tracked_files(root: Path) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
    return [root / name for name in out.stdout.decode().split("\0") if name]


def scan_text(name: str, text: str) -> list[tuple[str, str, int]]:
    hits: list[tuple[str, str, int]] = []
    is_test = "/tests/" in name
    for lineno, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            if kind == "password-assignment":
                value = match.group(2).strip()
                if PLACEHOLDER.search(value):
                    continue
                # 测试里的哨兵字符串通常是小写单词/连字符且无数字，如 do-not-return-this-secret；
                # 真实凭据一般含数字、大小写混合或符号。
                if is_test and re.fullmatch(r"[a-z][a-z-]{7,}", value):
                    continue
            if PLACEHOLDER.search(match.group(0)) and kind not in {"private-key", "jwt"}:
                continue
            hits.append((name, kind, lineno))
    return hits


def scan_worktree(root: Path) -> list[tuple[str, str, int]]:
    hits: list[tuple[str, str, int]] = []
    for path in tracked_files(root):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits.extend(scan_text(str(path.relative_to(root)), text))
    return hits


def scan_history(root: Path) -> list[tuple[str, str, int]]:
    """只看历史新增行（+），避免同一行在多个提交中重复计数。"""
    result = subprocess.run(
        ["git", "log", "-p", "--all", "--unified=0", "--no-color", "--format=@@COMMIT %H"],
        cwd=root, capture_output=True, check=True,
    )
    hits: list[tuple[str, str, int]] = []
    commit = ""
    path = ""
    for line in result.stdout.decode("utf-8", errors="ignore").splitlines():
        if line.startswith("@@COMMIT "):
            commit = line.split(" ", 1)[1][:12]
        elif line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            for name, kind, _ in scan_text(path, line[1:]):
                hits.append((f"{commit}:{name}", kind, 0))
    return hits


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", action="store_true", help="同时扫描 git 历史新增行")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]

    hits = scan_worktree(root)
    if args.history:
        hits += scan_history(root)

    if not hits:
        print("未在跟踪文件中发现硬编码凭据、私钥、JWT 或手机号等敏感信息。")
        return 0
    print(f"发现 {len(hits)} 处疑似敏感信息（仅列出位置与类型）：")
    for name, kind, lineno in hits:
        print(f"- {name}:{lineno} [{kind}]" if lineno else f"- {name} [{kind}]")
    return 1


if __name__ == "__main__":
    sys.exit(main())
