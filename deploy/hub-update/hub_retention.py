"""Hub 宿主机镜像与归档保留策略：默认只预览，显式确认后才删除。

边界：
- 只处理 ``<HUB_UPDATE_IMAGE_REPO>:<semver>`` 形式的本地镜像标签，以及归档目录内的
  ``hub-image-<version>.tar.gz``；
- 保护当前容器镜像、执行器 journal 中的目标/上一版本，以及最近 ``--keep`` 个版本；
- 绝不删除数据卷、容器或 compose 栈；``docker rmi`` 不使用 ``-f``，镜像仍被占用时
  如实记录失败，不升级为强制删除；
- 每次实际删除都追加到 ``HUB_UPDATE_STATE_DIR`` 下的审计日志。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable

from hub_updater import Config, UpdateError, _read_json, _utc_now

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$")
ARCHIVE_RE = re.compile(r"^hub-image-(\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?)\.tar\.gz$")
DEFAULT_KEEP = 3
PRUNE_CONFIRM = "prune"


def _version_key(tag: str) -> tuple | None:
    """语义化排序键：正式版高于同号预发布版；非版本标签返回 None。"""
    if not VERSION_RE.match(tag):
        return None
    core, _, pre = tag.partition("-")
    try:
        numbers = tuple(int(item) for item in core.split("."))
    except ValueError:
        return None
    return numbers + ((1,) if not pre else (0, pre))


def _run(args: list[str], timeout: float) -> tuple[int, str]:
    try:
        completed = subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)
    return completed.returncode, "\n".join(
        part for part in (completed.stdout, completed.stderr) if part
    ).strip()


class Retention:
    """按版本保留镜像标签与归档；plan() 永远只读，prune() 才删除。"""

    def __init__(
        self,
        config: Config,
        *,
        run: Callable[[list[str], float], tuple[int, str]] | None = None,
    ) -> None:
        self.config = config
        self.run = run or _run
        self.audit_path = config.state_dir / "hub-retention-audit.jsonl"

    # ---------- 采集 ----------

    def _tags(self) -> dict[str, str]:
        """返回版本标签 → 镜像 ID；仓库名由配置固定，不接受调用方传入。"""
        code, output = self.run(
            ["docker", "image", "ls", f"--filter=reference={self.config.image_repo}:*",
             "--format", "{{.Tag}} {{.ID}}"],
            60,
        )
        if code != 0:
            raise UpdateError(f"无法读取本地镜像：{output[:200]}")
        tags: dict[str, str] = {}
        for line in output.splitlines():
            tag, _, image_id = line.strip().partition(" ")
            tag, image_id = tag.strip(), image_id.strip()
            if _version_key(tag) is not None:
                tags[tag] = image_id
        return tags

    def _running_refs(self) -> set[str]:
        """所有容器（含已停止）使用的镜像引用/ID，避免删掉后续可恢复的镜像。"""
        code, output = self.run(["docker", "ps", "-a", "--format", "{{.Image}}"], 60)
        if code != 0:
            return set()
        return {line.strip() for line in output.splitlines() if line.strip()}

    def _protected_tags(self, tags: dict[str, str]) -> set[str]:
        journal = _read_json(self.config.state_dir / "hub-update.json", {}) or {}
        refs: set[str] = set()
        for key in ("target_version", "previous_version"):
            value = str(journal.get(key) or "").strip()
            if value:
                refs.add(value)
                refs.add(f"{self.config.image_repo}:{value}")
        refs |= self._running_refs()
        protected: set[str] = set()
        for ref in refs:
            if ref in tags:
                protected.add(ref)
                continue
            if ":" in ref:
                tag = ref.rsplit(":", 1)[-1]
                if tag in tags:
                    protected.add(tag)
                    continue
            # docker ps 可能输出镜像 ID；同 ID 的所有标签都要保护。
            protected |= {tag for tag, image_id in tags.items() if image_id and image_id == ref}
        return protected

    def _image_size(self, tag: str) -> int:
        code, output = self.run(
            ["docker", "image", "inspect", "--format", "{{.Size}}", f"{self.config.image_repo}:{tag}"],
            30,
        )
        if code != 0:
            return 0
        try:
            return int(output.strip().splitlines()[0])
        except (ValueError, IndexError):
            return 0

    def _archives(self, keep_versions: set[str]) -> list[dict]:
        archive_dir = self.config.archive_dir
        if not archive_dir.is_dir():
            return []
        items: list[dict] = []
        for path in sorted(archive_dir.iterdir()):
            match = ARCHIVE_RE.fullmatch(path.name)
            if not match or path.is_symlink() or not path.is_file():
                continue
            if match.group(1) in keep_versions:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            items.append({"name": path.name, "version": match.group(1), "size_bytes": size})
        return items

    # ---------- 预览与执行 ----------

    def plan(self, keep: int = DEFAULT_KEEP) -> dict:
        keep = max(1, int(keep))
        tags = self._tags()
        protected = self._protected_tags(tags)
        ordered = sorted(tags, key=_version_key, reverse=True)
        keep_versions = set(ordered[:keep]) | protected
        images = [
            {"tag": tag, "ref": f"{self.config.image_repo}:{tag}", "size_bytes": self._image_size(tag)}
            for tag in ordered
            if tag not in keep_versions
        ]
        archives = self._archives(keep_versions)
        image_total = sum(item["size_bytes"] for item in images)
        archive_total = sum(item["size_bytes"] for item in archives)
        return {
            "image_repo": self.config.image_repo,
            "keep": keep,
            "protected_tags": sorted(protected),
            "images": {"items": images, "reclaimable_bytes": image_total},
            "archives": {"items": archives, "reclaimable_bytes": archive_total},
            "reclaimable_bytes": image_total + archive_total,
            "generated_at": _utc_now(),
            "dry_run": True,
        }

    def prune(self, *, keep: int = DEFAULT_KEEP, confirm: str = "", dry_run: bool = True) -> dict:
        if dry_run:
            return {**self.plan(keep), "deleted": [], "failures": []}
        if confirm != PRUNE_CONFIRM:
            raise UpdateError("实际删除必须显式提交 --confirm prune")
        plan = self.plan(keep)
        deleted: list[dict] = []
        failures: list[dict] = []
        for item in plan["images"]["items"]:
            # 不用 -f：镜像仍被容器引用时删除会失败，保留失败原因而不是强制移除。
            code, output = self.run(["docker", "rmi", item["ref"]], 120)
            if code == 0:
                deleted.append({"scope": "images", **item})
            else:
                failures.append({"scope": "images", "ref": item["ref"], "error": output[:300]})
        archive_root = self.config.archive_dir
        for item in plan["archives"]["items"]:
            path = archive_root / str(item["name"])
            try:
                if path.is_symlink() or not path.is_file():
                    raise ValueError("归档文件不可信")
                if path.resolve().parent != archive_root.resolve():
                    raise ValueError("归档路径越界")
                path.unlink()
            except (OSError, ValueError) as exc:
                failures.append({"scope": "archives", "name": item["name"], "error": str(exc)})
                continue
            deleted.append({"scope": "archives", **item})
        result = {
            **plan,
            "dry_run": False,
            "deleted": deleted,
            "failures": failures,
            "freed_bytes": sum(int(row.get("size_bytes") or 0) for row in deleted),
        }
        self._audit(result)
        return result

    def _audit(self, result: dict) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "time": _utc_now(),
            "action": "prune",
            "result": "failed" if result["failures"] else "ok",
            "deleted": len(result["deleted"]),
            "failures": len(result["failures"]),
            "freed_bytes": result["freed_bytes"],
        }
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hub 宿主机镜像/归档保留策略（默认只预览）")
    parser.add_argument("--apply", action="store_true", help="实际删除；必须同时提供 --confirm prune")
    parser.add_argument("--confirm", default="", help="删除确认标记，固定为 prune")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="保留最近多少个版本")
    args = parser.parse_args(argv)
    config = Config()
    if ":" in config.image_repo.rsplit("/", 1)[-1]:
        print("HUB_UPDATE_IMAGE_REPO 不能自带版本标签", file=sys.stderr)
        return 2
    try:
        result = Retention(config).prune(
            keep=args.keep, confirm=args.confirm, dry_run=not args.apply
        )
    except UpdateError as exc:
        print(f"保留策略执行失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result.get("failures") else 1


if __name__ == "__main__":
    raise SystemExit(main())
