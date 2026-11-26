"""workspace_audit.py 的定向测试（Issue #105）。

重点覆盖“盘点失败不能被当成干净工作区”这一约束：缺失、锁定、不可访问、
独有提交与忽略文件都必须被显式标记。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "deploy" / "tools" / "workspace_audit.py"
SPEC = importlib.util.spec_from_file_location("workspace_audit", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
AUDIT = importlib.util.module_from_spec(SPEC)
# dataclass 在 exec 时需要能从 sys.modules 找到自身模块，否则解析注解会失败
sys.modules[SPEC.name] = AUDIT
SPEC.loader.exec_module(AUDIT)


def _run(cwd: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-b", "main")
    _run(repo, "commit", "--allow-empty", "-m", "init")
    # 用远端跟踪引用模拟已 fetch 的 origin/main，避免测试依赖网络
    _run(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def _row(rows: list[dict[str, object]], path: Path) -> dict[str, object]:
    target = path.resolve()
    return next(row for row in rows if Path(str(row["path"])).resolve() == target)


def test_clean_merged_worktree_is_reported_ok(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    rows, _ = AUDIT.audit(repo)

    row = _row(rows, repo)
    assert row["state"] == "ok"
    assert row["merged"] is True
    assert row["pushed"] is True
    assert row["unique"] == 0
    assert row["dirty"] == 0
    assert row["untracked"] == 0


def test_deleted_worktree_is_missing_not_clean(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    worktree = tmp_path / "wt"
    _run(repo, "worktree", "add", "-b", "feat/deleted", str(worktree))
    shutil.rmtree(worktree)

    rows, _ = AUDIT.audit(repo)

    row = _row(rows, worktree)
    assert row["state"] == "missing"
    # 关键约束：磁盘状态未知时不能输出 0，否则会被误读为干净
    assert row["dirty"] is None
    assert row["errors"]


def test_locked_worktree_is_flagged(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    worktree = tmp_path / "wt"
    _run(repo, "worktree", "add", "-b", "feat/locked", str(worktree))
    _run(repo, "worktree", "lock", "--reason", "in use", str(worktree))

    rows, _ = AUDIT.audit(repo)

    assert _row(rows, worktree)["state"] == "locked"


def test_broken_git_link_is_inaccessible_not_clean(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    worktree = tmp_path / "wt"
    _run(repo, "worktree", "add", "-b", "feat/broken", str(worktree))
    # Worktree 的 .git 是文件；删除后目录仍在，但 Git 无法再读取
    (worktree / ".git").unlink()

    rows, _ = AUDIT.audit(repo)

    row = _row(rows, worktree)
    assert row["state"] == "inaccessible"
    assert row["dirty"] is None
    assert row["errors"]


def test_dirty_untracked_and_ignored_are_counted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    tracked = repo / "tracked.txt"
    tracked.write_text("a", encoding="utf-8")
    _run(repo, "add", "tracked.txt")
    _run(repo, "commit", "-m", "track")
    _run(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    tracked.write_text("b", encoding="utf-8")
    (repo / "new.txt").write_text("n", encoding="utf-8")
    (repo / ".gitignore").write_text("cache/\n", encoding="utf-8")
    (repo / "cache").mkdir()
    (repo / "cache" / "blob.bin").write_text("x", encoding="utf-8")

    rows, _ = AUDIT.audit(repo)

    row = _row(rows, repo)
    assert row["dirty"] == 1
    assert row["untracked"] == 2  # new.txt 与 .gitignore
    assert row["ignored"] == 1


def test_unpushed_commit_is_unique_and_not_merged(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _run(repo, "commit", "--allow-empty", "-m", "local only")

    rows, _ = AUDIT.audit(repo)

    row = _row(rows, repo)
    assert row["unique"] == 1
    assert row["merged"] is False
    assert row["pushed"] is False


def test_missing_origin_main_warns_instead_of_silent_success(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-b", "main")
    _run(repo, "commit", "--allow-empty", "-m", "init")

    rows, warnings = AUDIT.audit(repo)

    assert any("origin/main" in warning for warning in warnings)
    assert _row(rows, repo)["merged"] is None


def test_non_git_root_is_rejected(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(ValueError):
        AUDIT.audit(plain)
