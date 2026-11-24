"""Hub 镜像/归档保留策略单测：只验证保留规则与删除边界，真实 docker 由运维验收。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from hub_retention import Retention
from hub_updater import Config, UpdateError

REPO = "menu-capture-hub"


def make_config(tmp_path: Path) -> Config:
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}\n", encoding="utf-8")
    return Config({
        "HUB_UPDATE_TOKEN": "t" * 32,
        "HUB_UPDATE_COMPOSE_FILE": str(compose),
        "HUB_UPDATE_PROJECT": "menu-capture",
        "HUB_UPDATE_STATE_DIR": str(tmp_path / "state"),
        "HUB_UPDATE_IMAGE_REPO": REPO,
    })


class FakeDocker:
    """按参数返回固定输出，并记录所有调用，确保不会出现 volume/rm/down 等危险命令。"""

    def __init__(self, tags: dict[str, str], running: list[str], sizes: dict[str, int] | None = None) -> None:
        self.tags = tags
        self.running = running
        self.sizes = sizes or {}
        self.calls: list[list[str]] = []
        self.fail_rmi: set[str] = set()

    def __call__(self, args: list[str], timeout: float) -> tuple[int, str]:
        self.calls.append(args)
        if args[:3] == ["docker", "image", "ls"]:
            return 0, "\n".join(f"{tag} {image_id}" for tag, image_id in self.tags.items())
        if args[:3] == ["docker", "ps", "-a"]:
            return 0, "\n".join(self.running)
        if args[:3] == ["docker", "image", "inspect"]:
            tag = args[-1].rsplit(":", 1)[-1]
            return 0, str(self.sizes.get(tag, 0))
        if args[:2] == ["docker", "rmi"]:
            ref = args[-1]
            if ref in self.fail_rmi:
                return 1, "image is being used by running container"
            return 0, f"Untagged: {ref}"
        return 0, ""


def write_journal(config: Config, **payload) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / "hub-update.json").write_text(json.dumps(payload), encoding="utf-8")


def test_plan_keeps_recent_and_protected_versions(tmp_path):
    config = make_config(tmp_path)
    write_journal(config, target_version="0.2.0", previous_version="0.1.0")
    docker = FakeDocker(
        {"0.4.0": "id4", "0.3.0": "id3", "0.2.0": "id2", "0.1.0": "id1"},
        running=[f"{REPO}:0.4.0"],
        sizes={"0.1.0": 42},
    )

    plan = Retention(config, run=docker).plan(keep=2)

    # 0.4.0 运行中、0.3.0 最近、0.2.0/0.1.0 来自 journal 的保护，全部保留。
    assert [item["tag"] for item in plan["images"]["items"]] == []
    assert set(plan["protected_tags"]) == {"0.1.0", "0.2.0", "0.4.0"}
    assert plan["dry_run"] is True
    assert not any(call[:2] == ["docker", "rmi"] for call in docker.calls)


def test_plan_marks_old_versions_when_not_protected(tmp_path):
    config = make_config(tmp_path)
    write_journal(config, target_version="", previous_version="")
    docker = FakeDocker(
        {"0.4.0": "id4", "0.3.0": "id3", "0.2.0": "id2", "0.1.0": "id1"},
        running=[],
        sizes={"0.2.0": 100, "0.1.0": 50},
    )

    plan = Retention(config, run=docker).plan(keep=2)

    assert [item["tag"] for item in plan["images"]["items"]] == ["0.2.0", "0.1.0"]
    assert plan["images"]["reclaimable_bytes"] == 150


def test_prune_requires_confirmation_and_dry_run_deletes_nothing(tmp_path):
    config = make_config(tmp_path)
    write_journal(config)
    docker = FakeDocker({"0.2.0": "id2", "0.1.0": "id1"}, running=[])
    retention = Retention(config, run=docker)

    preview = retention.prune(keep=1, dry_run=True)
    assert preview["dry_run"] is True and preview["deleted"] == []
    assert not any(call[:2] == ["docker", "rmi"] for call in docker.calls)

    with pytest.raises(UpdateError):
        retention.prune(keep=1, confirm="", dry_run=False)
    assert not any(call[:2] == ["docker", "rmi"] for call in docker.calls)


def test_prune_deletes_old_tag_and_reports_failures(tmp_path):
    config = make_config(tmp_path)
    write_journal(config)
    docker = FakeDocker({"0.3.0": "id3", "0.2.0": "id2", "0.1.0": "id1"}, running=[])
    docker.fail_rmi.add(f"{REPO}:0.1.0")

    result = Retention(config, run=docker).prune(keep=1, confirm="prune", dry_run=False)

    assert [row["tag"] for row in result["deleted"]] == ["0.2.0"]
    assert [row["ref"] for row in result["failures"]] == [f"{REPO}:0.1.0"]
    # 绝不出现强制删除、数据卷或 compose 破坏性操作。
    forbidden = {"-f", "volume", "down", "prune"}
    assert not any(forbidden & set(call) for call in docker.calls)
    audit = (config.state_dir / "hub-retention-audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(audit[-1])["result"] == "failed"


def test_archives_follow_same_retention_and_refuse_symlink_escape(tmp_path):
    config = make_config(tmp_path)
    write_journal(config)
    config.archive_dir.mkdir(parents=True, exist_ok=True)
    for version in ["0.3.0", "0.2.0", "0.1.0"]:
        (config.archive_dir / f"hub-image-{version}.tar.gz").write_bytes(b"x" * 10)
    outside = tmp_path / "outside.tar.gz"
    outside.write_bytes(b"secret")
    if hasattr(os, "symlink"):
        try:
            (config.archive_dir / "hub-image-0.0.9.tar.gz").symlink_to(outside)
        except OSError:
            pass
    docker = FakeDocker({"0.3.0": "id3"}, running=[])

    plan = Retention(config, run=docker).plan(keep=1)
    assert [item["version"] for item in plan["archives"]["items"]] == ["0.1.0", "0.2.0"]

    result = Retention(config, run=docker).prune(keep=1, confirm="prune", dry_run=False)
    assert sorted(row["version"] for row in result["deleted"] if row["scope"] == "archives") == ["0.1.0", "0.2.0"]
    assert (config.archive_dir / "hub-image-0.3.0.tar.gz").is_file()
    assert outside.is_file()
