"""Windows 更新执行器：安装前校验、安装编排、健康验证与自动回滚。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from menu_capture.updates.update_install import (
    JOB_SCHEMA,
    InstallController,
    InstallOrchestrator,
    _check_publisher,
    verify_artifact,
)


def make_release(root: Path, version: str, entry: str = "assets/index.js") -> Path:
    release = root / "releases" / version
    (release / "python").mkdir(parents=True, exist_ok=True)
    (release / "python" / "python.exe").write_bytes(b"python")
    (release / "app" / "menu_capture" / "api").mkdir(parents=True, exist_ok=True)
    (release / "app" / "menu_capture" / "api" / "server.py").write_text("", encoding="utf-8")
    frontend = release / "app" / "web" / "react" / entry
    frontend.parent.mkdir(parents=True, exist_ok=True)
    frontend.write_bytes(b"frontend")
    (release / "release-manifest.json").write_text(json.dumps({
        "version": version,
        "frontend_entry": entry,
        "frontend_sha256": hashlib.sha256(b"frontend").hexdigest(),
    }), encoding="utf-8")
    return release


def make_artifact(tmp_path: Path, version: str = "0.3.0", body: bytes = b"installer-bytes"):
    name = f"MenuCapture-v{version}.exe"
    path = tmp_path / "download" / version / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return {
        "status": "completed",
        "version": version,
        "asset_kind": "windows_installer",
        "asset_name": name,
        "arch": "x64",
        "sha256": hashlib.sha256(body).hexdigest(),
        "total": len(body),
        "path": str(path),
    }


def make_install_root(tmp_path: Path, previous: str = "0.2.2") -> Path:
    root = tmp_path / "install"
    make_release(root, previous)
    (root / "rollback.ps1").write_text("rollback", encoding="utf-8")
    (root / "current-version").write_text(previous, encoding="utf-8")
    return root


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_orchestrator(tmp_path: Path, *, run_command, probe, health_timeout=0.05,
                      publisher=None):
    root = make_install_root(tmp_path)
    state = make_artifact(tmp_path)
    job = {
        "schema": JOB_SCHEMA,
        "artifact_path": state["path"],
        "asset_name": state["asset_name"],
        "asset_kind": state["asset_kind"],
        "sha256": state["sha256"],
        "size": state["total"],
        "download_dir": str(tmp_path / "download"),
        "data_dir": str(tmp_path / "data"),
        "target_version": "0.3.0",
        "previous_version": "0.2.2",
        "install_root": str(root),
        "port": 8765,
    }
    clock = FakeClock()

    def sleep(seconds: float) -> None:
        clock.now += seconds

    path = tmp_path / "data" / "update-install.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return InstallOrchestrator(
        job=job, journal_path=path, run_command=run_command, probe_version=probe,
        verify_publisher=publisher or (lambda artifact: {"status": "valid", "signer": "CN=Test"}),
        sleep=sleep, clock=clock, health_timeout=health_timeout,
    ), root, state


def test_verify_artifact_accepts_completed_identity(tmp_path):
    state = make_artifact(tmp_path)
    artifact = verify_artifact(tmp_path / "download", state, expected_version="0.3.0",
                               platform="windows", arch="x64")
    assert artifact["sha256"] == state["sha256"]
    assert artifact["path"] == state["path"]


@pytest.mark.parametrize("field,value,message", [
    ("status", "downloading", "尚未完成下载"),
    ("version", "0.2.9", "版本与安装目标不一致"),
    ("asset_kind", "hub_image", "平台与本机不一致"),
    ("arch", "arm64", "架构与本机不一致"),
])
def test_verify_artifact_rejects_wrong_identity(tmp_path, field, value, message):
    state = make_artifact(tmp_path)
    state[field] = value
    with pytest.raises(ValueError, match=message):
        verify_artifact(tmp_path / "download", state, expected_version="0.3.0",
                        platform="windows", arch="x64")


def test_verify_artifact_rejects_tampered_file_and_foreign_path(tmp_path):
    state = make_artifact(tmp_path)
    Path(state["path"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="大小|SHA-256"):
        verify_artifact(tmp_path / "download", state, expected_version="0.3.0",
                        platform="windows", arch="x64")

    state = make_artifact(tmp_path)
    outside = tmp_path / "outside.exe"
    outside.write_bytes(b"installer-bytes")
    state["path"] = str(outside)
    with pytest.raises(ValueError, match="路径与版本目录不一致"):
        verify_artifact(tmp_path / "download", state, expected_version="0.3.0",
                        platform="windows", arch="x64")


def test_orchestrator_installs_verifies_and_completes(tmp_path):
    root = make_install_root(tmp_path)
    state = make_artifact(tmp_path)

    def run(args, timeout):
        assert args[0] == state["path"]
        assert "/VERYSILENT" in args
        # 安装目录必须显式传给安装器，不能依赖默认路径或历史注册表值。
        # 参数不能自带引号：Python 会按 CreateProcess 规则再次转义，装成 /DIR=\"...\" 后 Inno 解析失败。
        assert f"/DIR={root}" in args
        assert not any("=" in item and '"' in item for item in args)
        make_release(root, "0.3.0")
        (root / "current-version").write_text("0.3.0", encoding="utf-8")
        return 0, ""

    orchestrator, _, _ = make_orchestrator(tmp_path, run_command=run, probe=lambda port: "0.3.0")
    journal = orchestrator.execute()

    assert journal["status"] == "completed"
    assert journal["message"] == "已切换到 0.3.0"
    assert journal["finished_at"]


def test_orchestrator_fails_without_rollback_when_installer_fails(tmp_path):
    calls: list[str] = []

    def run(args, timeout):
        calls.append(args[0])
        return 1, "setup failed"

    orchestrator, root, _ = make_orchestrator(tmp_path, run_command=run, probe=lambda port: "0.2.2")
    journal = orchestrator.execute()

    assert journal["status"] == "failed"
    assert "安装程序退出码 1" in journal["error"]
    assert (root / "current-version").read_text(encoding="utf-8") == "0.2.2"
    assert all("rollback.ps1" not in call for call in calls)


def test_orchestrator_rolls_back_when_health_check_times_out(tmp_path):
    root = make_install_root(tmp_path)
    state = make_artifact(tmp_path)
    current = {"version": "0.2.9"}

    def run(args, timeout):
        if args[0] == state["path"]:
            make_release(root, "0.3.0")
            (root / "current-version").write_text("0.3.0", encoding="utf-8")
            return 0, ""
        if "rollback.ps1" in " ".join(args):
            current["version"] = "0.2.2"
            (root / "current-version").write_text("0.2.2", encoding="utf-8")
            return 0, "rolled back"
        return 1, "unexpected"

    orchestrator, _, _ = make_orchestrator(tmp_path, run_command=run, probe=lambda port: current["version"])
    journal = orchestrator.execute()

    assert journal["status"] == "rolled_back"
    assert "已回滚到 0.2.2" in journal["message"]


def test_orchestrator_reports_failed_when_rollback_fails(tmp_path):
    root = make_install_root(tmp_path)
    state = make_artifact(tmp_path)

    def run(args, timeout):
        if args[0] == state["path"]:
            make_release(root, "0.3.0")
            (root / "current-version").write_text("0.3.0", encoding="utf-8")
            return 0, ""
        return 1, "rollback exploded"

    orchestrator, _, _ = make_orchestrator(tmp_path, run_command=run, probe=lambda port: "0.2.9")
    journal = orchestrator.execute()

    assert journal["status"] == "failed"
    assert "回滚失败" in journal["message"]


def test_orchestrator_refuses_tampered_artifact_before_running_installer(tmp_path):
    calls: list[str] = []
    orchestrator, _, state = make_orchestrator(
        tmp_path, run_command=lambda args, timeout: calls.append(args[0]) or (0, ""),
        probe=lambda port: "0.3.0",
    )
    # 保持与清单相同大小，确保触发的是 SHA-256 校验而不是大小校验。
    Path(state["path"]).write_bytes(b"tampered-bytes!")

    journal = orchestrator.execute()

    assert journal["status"] == "failed"
    assert "SHA-256" in journal["error"]
    assert calls == []


def make_controller(tmp_path: Path, *, supported=True, latest_sha: str | None = None,
                    idle=(True, ""), current="0.2.2", journal: dict | None = None,
                    pid_alive=None):
    root = make_install_root(tmp_path, current)
    state = make_artifact(tmp_path)
    latest = {
        "version": "0.3.0",
        "eligible": True,
        "assets": [{
            "kind": "windows_installer",
            "name": state["asset_name"],
            "sha256": latest_sha or state["sha256"],
            "size": state["total"],
        }],
    }
    commands: list[tuple[list[str], dict]] = []

    def fake_popen(command, **kwargs):
        commands.append((command, kwargs))
        return object()

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    if journal is not None:
        (data_dir / "update-install.json").write_text(json.dumps(journal), encoding="utf-8")
    controller = InstallController(
        download_dir=tmp_path / "download",
        install_root=root,
        data_dir=data_dir,
        role="worker",
        platform="windows",
        arch="x64",
        port=8765,
        latest_provider=lambda: latest,
        artifact_provider=lambda: state,
        current_version=lambda: current,
        idle_check=lambda: idle,
        supported=supported,
        popen=fake_popen,
        python="python.exe",
        app_dir=tmp_path,
        pid_alive=pid_alive or (lambda pid: True),
    )
    return controller, commands, root


def test_controller_requires_confirmation_and_idle(tmp_path):
    controller, commands, _ = make_controller(tmp_path)
    with pytest.raises(ValueError, match="确认要安装的版本号"):
        controller.start("0.3.0", "0.2.9")
    controller, commands, _ = make_controller(tmp_path, idle=(False, "采集仍在执行"))
    with pytest.raises(ValueError, match="采集仍在执行"):
        controller.start("0.3.0", "0.3.0")
    assert commands == []


def test_controller_rejects_artifact_not_matching_manifest(tmp_path):
    controller, commands, _ = make_controller(tmp_path, latest_sha="f" * 64)
    with pytest.raises(ValueError, match="发布清单不一致"):
        controller.start("0.3.0", "0.3.0")
    assert commands == []


def test_controller_rejects_unknown_previous_version(tmp_path):
    controller, _, _ = make_controller(tmp_path, current="")
    with pytest.raises(ValueError, match="无法确定当前运行版本"):
        controller.start("0.3.0", "0.3.0")


def test_controller_spawns_detached_install_and_records_job(tmp_path):
    controller, commands, _ = make_controller(tmp_path)

    status = controller.start("0.3.0", "0.3.0")

    assert status["active"] is True and status["status"] == "starting"
    assert len(commands) == 1
    command, kwargs = commands[0]
    assert command[1:4] == ["-m", "menu_capture.updates.update_install", "--job"]
    assert kwargs["cwd"] == str(tmp_path)
    job = json.loads((Path(tmp_path) / "data" / "update-install-job.json").read_text(encoding="utf-8"))
    assert job["target_version"] == "0.3.0"
    assert job["previous_version"] == "0.2.2"
    assert job["install_root"].endswith("install")


def test_controller_rejects_when_unsupported(tmp_path):
    controller, commands, _ = make_controller(tmp_path, supported=False)
    with pytest.raises(ValueError, match="未启用自动安装"):
        controller.start("0.3.0", "0.3.0")
    assert commands == []


def test_controller_rejects_concurrent_install(tmp_path):
    controller, commands, _ = make_controller(tmp_path, journal={
        "status": "installing",
        "target_version": "0.3.0",
    })
    with pytest.raises(ValueError, match="已有安装任务"):
        controller.start("0.3.0", "0.3.0")
    assert commands == []


def test_controller_job_contract_runs_in_orchestrator(tmp_path):
    """回归：控制器写出的任务文件必须能被独立执行器直接消费。"""
    root = make_install_root(tmp_path)
    state = make_artifact(tmp_path)
    controller, commands, _ = make_controller(tmp_path)
    controller.start("0.3.0", "0.3.0")
    assert len(commands) == 1

    job_path = tmp_path / "data" / "update-install-job.json"
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["schema"] == JOB_SCHEMA
    assert job["artifact_path"] == state["path"]
    assert job["asset_name"] == state["asset_name"]

    def run(args, timeout):
        assert args[0] == job["artifact_path"]
        make_release(root, "0.3.0")
        (root / "current-version").write_text("0.3.0", encoding="utf-8")
        return 0, ""

    orchestrator = InstallOrchestrator(
        job=job,
        journal_path=tmp_path / "data" / "update-install.json",
        run_command=run,
        probe_version=lambda port: "0.3.0",
        verify_publisher=lambda artifact: {"status": "valid", "signer": "CN=Test"},
        sleep=lambda seconds: None,
        clock=FakeClock(),
        health_timeout=0.05,
    )
    journal = orchestrator.execute()

    assert journal["status"] == "completed"
    assert journal["message"] == "已切换到 0.3.0"


def test_verify_artifact_rejects_traversal_and_symlinked_download(tmp_path):
    state = make_artifact(tmp_path)
    state["asset_name"] = "../evil.exe"
    with pytest.raises(ValueError, match="名称无效"):
        verify_artifact(tmp_path / "download", state, expected_version="0.3.0",
                        platform="windows", arch="x64")

    # 版本目录被替换成指向别处的链接时，不能顺着链接读文件。
    download = tmp_path / "download"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "MenuCapture-v0.3.0.exe").write_bytes(b"installer-bytes")
    (download / "0.3.0").rename(tmp_path / "real-0.3.0")
    try:
        (download / "0.3.0").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")
    state = make_artifact(tmp_path)
    state["path"] = str(outside / state["asset_name"])
    with pytest.raises(ValueError, match="符号链接|重解析点"):
        verify_artifact(download, state, expected_version="0.3.0",
                        platform="windows", arch="x64")


def test_orchestrator_rejects_unsigned_artifact_without_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", raising=False)
    calls: list[str] = []
    orchestrator, _, _ = make_orchestrator(
        tmp_path, run_command=lambda args, timeout: calls.append(args[0]) or (0, ""),
        probe=lambda port: "0.3.0",
        publisher=lambda artifact: {"status": "unsigned"},
    )
    journal = orchestrator.execute()

    assert journal["status"] == "failed"
    assert "可信发布证明" in journal["error"]
    assert calls == []


def test_check_publisher_allows_unsigned_only_with_explicit_flag(monkeypatch):
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", "1")
    assert _check_publisher({"status": "unsigned"})["status"] == "unsigned"
    with pytest.raises(ValueError, match="数字签名无效"):
        _check_publisher({"status": "invalid:hashmismatch"})
    with pytest.raises(ValueError, match="数字签名无效"):
        _check_publisher({"status": "unknown"})


def test_orchestrator_blocks_rollback_when_schema_changed(tmp_path):
    root = make_install_root(tmp_path)
    state = make_artifact(tmp_path)
    data_dir = tmp_path / "data"
    db_path = data_dir / "tasks.sqlite3"
    data_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE tasks (id TEXT)")
    calls: list[str] = []

    def run(args, timeout):
        calls.append(args[0])
        if args[0] == state["path"]:
            make_release(root, "0.3.0")
            (root / "current-version").write_text("0.3.0", encoding="utf-8")
            # 模拟新版本启动时执行了前向迁移。
            with sqlite3.connect(db_path) as db:
                db.execute("CREATE TABLE task_events (id TEXT)")
            return 0, ""
        return 1, "rollback exploded"

    orchestrator, _, _ = make_orchestrator(tmp_path, run_command=run, probe=lambda port: "0.2.9")
    journal = orchestrator.execute()

    assert journal["status"] == "failed"
    assert "已阻止自动回滚" in journal["message"]
    assert all("rollback.ps1" not in call for call in calls)


def test_orchestrator_does_not_accept_wrong_role_after_switch(tmp_path):
    root = make_install_root(tmp_path)
    state = make_artifact(tmp_path)

    def run(args, timeout):
        if args[0] == state["path"]:
            make_release(root, "0.3.0")
            (root / "current-version").write_text("0.3.0", encoding="utf-8")
            return 0, ""
        return 0, "rolled back"

    orchestrator, _, _ = make_orchestrator(
        tmp_path, run_command=run,
        probe=lambda port: {"version": "0.3.0", "role": "hub", "ok": True, "frontend": "index.js"},
    )
    journal = orchestrator.execute()

    assert journal["status"] == "failed"
    assert "回滚失败" in journal["message"]


def test_controller_recovers_stale_install_journal(tmp_path):
    controller, _, _ = make_controller(
        tmp_path, journal={"status": "installing", "pid": 4242, "target_version": "0.3.0"},
        pid_alive=lambda pid: False,
    )
    controller.recover()
    status = controller.status()

    assert status["status"] == "failed"
    assert "异常退出" in status["message"]
    assert status["active"] is False


def test_controller_recovery_accepts_already_switched_version(tmp_path):
    controller, _, _ = make_controller(
        tmp_path, current="0.3.0",
        journal={"status": "verifying", "pid": 4242, "target_version": "0.3.0"},
        pid_alive=lambda pid: False,
    )
    controller.recover()

    assert controller.status()["status"] == "completed"


def test_controller_keeps_journal_while_executor_alive(tmp_path):
    controller, _, _ = make_controller(
        tmp_path, journal={"status": "installing", "pid": 4242, "target_version": "0.3.0"},
        pid_alive=lambda pid: True,
    )
    controller.recover()

    assert controller.status()["status"] == "installing"
    assert controller.active() is True
