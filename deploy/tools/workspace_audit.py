#!/usr/bin/env python3
"""开发工作区只读盘点（Issue #105）。

用途：回收 Worktree 前逐个确认合并状态、独有提交、脏文件、未跟踪与忽略文件，
避免凭目录名判断。脚本只读，不删除、不移动、不改动任何 Git 引用。

设计约束：
- Git 命令失败时输出“未知”或错误状态，绝不把失败当成干净工作区。
- 缺失、锁定、失效（prunable）、不可访问的 Worktree 分别标记。
- 盘点结果只是判断依据，不等于删除许可；价值判断需人工确认。

用法：
    python3 deploy/tools/workspace_audit.py          # 表格输出
    python3 deploy/tools/workspace_audit.py --json   # 机器可读输出

退出码：0 正常；1 根路径不是 Git 仓库；2 有 Worktree 无法完整盘点。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

UNKNOWN_TEXT = "?"
MISSING_TEXT = "-"


@dataclass(frozen=True)
class GitResult:
    """Git 命令结果；ok 为 False 时 out 可能为空，错误原因在 err。"""

    ok: bool
    returncode: int
    out: str
    err: str


def git(args: list[str], cwd: Path | None = None) -> GitResult:
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
        # 只保留首行错误，避免整段 usage 淹没表格输出
        message = (result.stderr or result.stdout or "").strip().splitlines()
        return GitResult(False, result.returncode, "", message[0] if message else f"git {' '.join(args)} 失败")
    return GitResult(True, result.returncode, result.stdout.strip(), "")


def _int_or_none(result: GitResult) -> int | None:
    return int(result.out) if result.ok and result.out.lstrip("-").isdigit() else None


def list_worktrees(root: Path) -> tuple[list[dict[str, str]], str | None]:
    """解析 git worktree list，附带 locked / prunable / bare 标记。"""
    result = git(["worktree", "list", "--porcelain"], root)
    if not result.ok:
        return [], result.err

    items: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in result.out.splitlines():
        if line.startswith("worktree "):
            if current:
                items.append(current)
            current = {"path": line[len("worktree ") :]}
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD ") :]
        elif line.startswith("branch "):
            current["branch"] = line[len("branch ") :].replace("refs/heads/", "")
        elif line == "detached":
            current["branch"] = "(detached)"
        elif line == "bare":
            current["bare"] = "1"
        elif line == "locked" or line.startswith("locked "):
            # porcelain 格式：锁定原因紧跟在 locked 之后，可能为空
            current["locked"] = line[len("locked") :].strip()
        elif line == "prunable" or line.startswith("prunable "):
            current["prunable"] = line[len("prunable") :].strip()
    if current:
        items.append(current)
    return items, None


def inspect(path: Path, head: str, markers: dict[str, str]) -> dict[str, object]:
    """盘点单个 Worktree；任一 Git 命令失败都记录错误并输出未知值。"""
    errors: list[str] = []
    row: dict[str, object] = {
        "path": str(path),
        "branch": markers.get("branch", ""),
        "head": head[:7],
        "state": "ok",
        "flags": [key for key in ("locked", "prunable", "bare") if key in markers],
        "errors": errors,
        "upstream": None,
        "base": "origin/main",
        "ahead": None,
        "behind": None,
        "merged": None,
        "pushed": None,
        "unique": None,
        "dirty": None,
        "untracked": None,
        "ignored": None,
    }

    if "bare" in markers:
        row["state"] = "bare"
        return row
    if not path.exists():
        # 目录已删但注册仍在：git worktree list 会列为 prunable，这里显式标记缺失
        row["state"] = "missing"
        errors.append("路径不存在，需 git worktree prune 清理注册")
        return row

    status = git(["status", "--porcelain", "--ignored=traditional"], path)
    if not status.ok:
        # 目录存在但 Git 不可用（.git 损坏、权限不足、被替换成普通目录）
        row["state"] = "inaccessible"
        errors.append(status.err)
        if "locked" in markers:
            row["state"] = "locked"
        return row

    if "prunable" in markers:
        row["state"] = "prunable"
    elif "locked" in markers:
        row["state"] = "locked"

    status_lines = status.out.splitlines() if status.out else []
    row["dirty"] = sum(1 for line in status_lines if line[:2] not in ("??", "!!"))
    row["untracked"] = sum(1 for line in status_lines if line.startswith("??"))
    # traditional 模式按目录折叠，数量含目录本身；仅为提示，不是逐文件统计
    row["ignored"] = sum(1 for line in status_lines if line.startswith("!!"))

    upstream = git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], path)
    base = upstream.out if upstream.ok else "origin/main"
    row["upstream"] = upstream.out if upstream.ok else None
    row["base"] = base

    counts = git(["rev-list", "--left-right", "--count", f"HEAD...{base}"], path)
    if counts.ok:
        parts = counts.out.split()
        if len(parts) == 2:
            row["ahead"], row["behind"] = int(parts[0]), int(parts[1])
    else:
        errors.append(f"无法比较 {base}：{counts.err}")

    merged = git(["merge-base", "--is-ancestor", "HEAD", "origin/main"], path)
    if merged.ok:
        row["merged"] = True
    elif merged.returncode == 1:
        row["merged"] = False
    else:
        # 退出码非 0/1 属于真实错误（缺对象、缺引用），保持未知
        errors.append(f"无法判断是否已合入 origin/main：{merged.err}")

    contains = git(["branch", "-r", "--contains", "HEAD"], path)
    if contains.ok:
        row["pushed"] = bool(contains.out)
    else:
        errors.append(f"无法判断是否已推送：{contains.err}")

    unique = git(["rev-list", "--count", "HEAD", "--not", "--remotes"], path)
    row["unique"] = _int_or_none(unique)
    if row["unique"] is None:
        errors.append(f"无法统计独有提交：{unique.err}")

    return row


def audit(root: Path) -> tuple[list[dict[str, object]], list[str]]:
    """返回 (各 Worktree 盘点行, 全局警告)。非 Git 仓库时抛 ValueError。"""
    if not git(["rev-parse", "--git-common-dir"], root).ok:
        raise ValueError(f"不是 Git 仓库：{root}")

    worktrees, error = list_worktrees(root)
    if error:
        raise ValueError(f"无法列出 Worktree：{error}")

    warnings: list[str] = []
    if not git(["rev-parse", "--verify", "origin/main"], root).ok:
        # 缺引用不终止盘点，但合并状态与独有提交都会不可信
        warnings.append("缺少 origin/main，合并状态与独有提交无法完整判断；请先 git fetch origin")
    warnings.append("结果基于本地远端跟踪引用；判断前请先 git fetch 复核，避免引用陈旧导致误判")

    rows = [inspect(Path(item["path"]), item.get("head", ""), item) for item in worktrees]
    return rows, warnings


def _fmt(value: object, kind: str = "int") -> str:
    if kind == "bool":
        return MISSING_TEXT if value is None else ("yes" if value else "no")
    return MISSING_TEXT if value is None else str(value)


def render(rows: list[dict[str, object]], warnings: list[str]) -> None:
    for warning in warnings:
        print(f"警告：{warning}", file=sys.stderr)

    header = (
        f"{'状态':<13} {'分支':<38} {'HEAD':<9} {'ahead':>5} {'behind':>6} "
        f"{'unique':>6} {'merged':>6} {'pushed':>6} {'dirty':>5} {'untrack':>7} {'ignore':>6}  路径"
    )
    print(header)
    for row in rows:
        state = str(row["state"])
        branch = str(row["branch"])
        head = str(row["head"])
        print(
            f"{state:<13} {branch[:38]:<38} {head:<9} "
            f"{_fmt(row['ahead']):>5} {_fmt(row['behind']):>6} {_fmt(row['unique']):>6} "
            f"{_fmt(row['merged'], 'bool'):>6} {_fmt(row['pushed'], 'bool'):>6} "
            f"{_fmt(row['dirty']):>5} {_fmt(row['untracked']):>7} {_fmt(row['ignored']):>6}  {row['path']}"
        )

    error_rows = [row for row in rows if row["errors"]]
    if error_rows:
        print("\n盘点异常：", file=sys.stderr)
        for row in error_rows:
            for message in row["errors"]:
                print(f"  {row['path']}: {message}", file=sys.stderr)

    print(
        "\n说明：ahead/behind 相对 upstream（无 upstream 时为 origin/main），unique 为不在任何远端引用中的提交数；\n"
        "      ignore 按目录折叠，仅作提示。状态 ok 且 merged=yes、pushed=yes、unique=0、dirty=0、untracked=0 仍需\n"
        "      人工确认 ignored 内容价值。盘点结果不等于删除许可；删除前复核被忽略文件与运行进程引用。"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="开发工作区只读盘点")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--repo", default=".", help="主仓或任意 Worktree 路径")
    args = parser.parse_args()

    root = Path(args.repo).resolve()
    try:
        rows, warnings = audit(root)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"worktrees": rows, "warnings": warnings}, ensure_ascii=False, indent=2))
    else:
        render(rows, warnings)

    # 存在无法完整盘点的 Worktree 时以 2 退出，便于调用方识别，而不会误认为全部干净
    return 2 if any(row["state"] in ("missing", "inaccessible", "prunable") for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
