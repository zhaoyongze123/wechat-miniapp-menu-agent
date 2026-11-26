#!/usr/bin/env python3
"""开发工作区只读盘点（Issue #105）。

用途：回收 Worktree 前逐个确认合并状态、独有提交、脏文件与未跟踪文件，
避免凭目录名判断。脚本只读，不删除、不移动、不改动任何 Git 引用。

用法：
    python3 deploy/tools/workspace_audit.py          # 表格输出
    python3 deploy/tools/workspace_audit.py --json   # 机器可读输出
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def git(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        # Git 在 Windows 控制台默认使用本地编码，统一按 UTF-8 读取避免中文路径乱码
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def list_worktrees(root: Path) -> list[dict[str, str]]:
    raw = git(["worktree", "list", "--porcelain"], root)
    items: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in raw.splitlines():
        if line.startswith("worktree "):
            if current:
                items.append(current)
            current = {"path": line[len("worktree "):]}
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):].replace("refs/heads/", "")
        elif line == "detached":
            current["branch"] = "(detached)"
    if current:
        items.append(current)
    return items


def inspect(path: Path, head: str) -> dict[str, object]:
    ahead = git(["rev-list", "--count", "origin/main..HEAD"], path)
    behind = git(["rev-list", "--count", "HEAD..origin/main"], path)
    merged = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"],
        cwd=path,
        check=False,
        capture_output=True,
    ).returncode == 0
    status = git(["status", "--porcelain"], path).splitlines()
    # 优先用 git bundle/patch 保护；这里只统计，不判断内容价值
    return {
        "head": head[:7],
        "ahead": int(ahead) if ahead.isdigit() else -1,
        "behind": int(behind) if behind.isdigit() else -1,
        "merged": merged,
        "dirty": sum(1 for line in status if not line.startswith("??")),
        "untracked": sum(1 for line in status if line.startswith("??")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="开发工作区只读盘点")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--repo", default=".", help="主仓或任意 Worktree 路径")
    args = parser.parse_args()

    root = Path(args.repo).resolve()
    common = git(["rev-parse", "--git-common-dir"], root)
    if not common:
        print(f"不是 Git 仓库：{root}", file=sys.stderr)
        return 1
    # origin/main 可能未 fetch，先提示而不是静默给出错误结论
    if not git(["rev-parse", "--verify", "origin/main"], root):
        print("缺少 origin/main，请先执行 git fetch origin", file=sys.stderr)
        return 1

    rows = []
    for item in list_worktrees(root):
        info = inspect(Path(item["path"]), item.get("head", ""))
        rows.append({"path": item["path"], "branch": item.get("branch", ""), **info})

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print(f"{'分支':<46} {'HEAD':<9} {'ahead':>5} {'behind':>6} {'merged':>6} {'dirty':>5} {'untracked':>9}  路径")
    for row in rows:
        print(
            f"{row['branch']:<46} {row['head']:<9} {row['ahead']:>5} {row['behind']:>6} "
            f"{'yes' if row['merged'] else 'no':>6} {row['dirty']:>5} {row['untracked']:>9}  {row['path']}"
        )
    print("\n提示：merged=yes 且 dirty/untracked=0 才可直接 git worktree remove；否则先保护（备份分支 / Bundle / 补丁）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
