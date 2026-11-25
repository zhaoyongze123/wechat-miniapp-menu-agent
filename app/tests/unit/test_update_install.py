"""Windows 更新执行器：安装前校验、安装编排、健康验证与自动回滚。"""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from menu_capture.updates.update_install import (
    JOB_SCHEMA,
    MAX_ACTIVE_SECONDS,
    STALLED_MESSAGE,
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
    # 根级启动/回滚脚本是既有安装布局的一部分，补丁登记版本也依赖 Launch.ps1。
    (root / "Launch.ps1").write_text("launch", encoding="utf-8")
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


def artifact_latest(state: dict, sha256: str | None = None) -> dict:
    """构造与产物一致的已验证发布清单，供安装与更新服务共用。"""
    return {
        "version": "0.3.0",
        "eligible": True,
        "assets": [{
            "kind": "windows_installer",
            "name": state["asset_name"],
            "sha256": sha256 or state["sha256"],
            "size": state["total"],
        }],
    }


def completed_download_state(state: dict) -> dict:
    """构造与产物一致、已完成的下载状态，供测试走真实的 artifact 提供方。"""
    return {
        "active": False,
        "status": "completed",
        "version": "0.3.0",
        "asset_kind": "windows_installer",
        "asset_name": state["asset_name"],
        "arch": "x64",
        "sha256": state["sha256"],
        "total": state["total"],
        "path": state["path"],
    }


def make_controller(tmp_path: Path, *, supported=True, latest_sha: str | None = None,
                    idle=(True, ""), current="0.2.2", journal: dict | None = None,
                    pid_alive=None, bind: dict | None = None,
                    latest_provider=None, artifact_provider=None,
                    authorization_check=None, authorization_release=None,
                    filesystem_lock=None, popen=None):
    root = make_install_root(tmp_path, current)
    state = make_artifact(tmp_path)
    latest = artifact_latest(state, latest_sha)
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
        latest_provider=latest_provider or (lambda: latest),
        artifact_provider=artifact_provider or (lambda: state),
        current_version=lambda: current,
        idle_check=lambda: idle,
        supported=supported,
        popen=popen or fake_popen,
        python="python.exe",
        app_dir=tmp_path,
        pid_alive=pid_alive or (lambda pid: True),
        authorization_check=authorization_check,
        authorization_release=authorization_release,
        filesystem_lock=filesystem_lock,
    )
    if bind is not None:
        # 供回调在内部反查控制器自身状态（例如确认调用时没有持安装锁）。
        bind["controller"] = controller
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


def test_controller_leaves_journal_to_executor_after_spawn(tmp_path):
    """回归：spawn 后 journal 归执行器独占，父进程不再读-改-写追加 pid。

    父子进程共用同一个 .tmp 临时文件，父进程回写会覆盖执行器刚写入的进度。
    """
    class Process:
        pid = 4321

    controller, commands, _ = make_controller(
        tmp_path, popen=lambda command, **kwargs: Process(),
    )

    controller.start("0.3.0", "0.3.0")

    journal = json.loads((tmp_path / "data" / "update-install.json").read_text(encoding="utf-8"))
    assert "pid" not in journal
    assert journal["status"] == "starting"
    # 子进程登记 pid 之前仍保持互斥，不会因为暂时没有 pid 被判定为异常退出。
    assert controller.status()["active"] is True


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


def test_real_callbacks_do_not_deadlock_under_forced_interleaving(tmp_path):
    """确定性 ABBA 回归：真实 UpdateService 与 InstallController 双向回调。

    两个事件把两侧固定在“刚进入对方状态读取回调”的位置，不依赖随机调度。
    修复前：安装线程持安装锁等更新锁、状态线程持更新锁等安装锁，必然互锁；
    修复后两侧都不在持对方锁时进入回调，因此能正常完成。
    """
    from menu_capture.updates.updates import UpdateService

    state = make_artifact(tmp_path)
    service = UpdateService(
        tmp_path / "runtime", role="worker", platform="windows", machine="AMD64",
    )
    service._cache = {"checked_at": time.time(), "latest": artifact_latest(state)}
    service._download_state = completed_download_state(state)

    in_latest = threading.Event()
    in_snapshot = threading.Event()

    def latest_provider() -> dict:
        in_latest.set()
        assert in_snapshot.wait(5), "状态线程未进入安装快照回调"
        # 与生产一致：最新版本来自更新服务，会进入更新服务锁。
        return service.status_no_check().get("latest") or {}

    controller, commands, _ = make_controller(
        tmp_path, latest_provider=latest_provider,
        artifact_provider=service.download_status,
    )

    def snapshot() -> dict:
        in_snapshot.set()
        assert in_latest.wait(5), "安装线程未进入最新版本回调"
        return controller.status()

    service.install_snapshot_provider = snapshot
    errors: list[BaseException] = []
    results: dict[str, object] = {}

    def status_thread() -> None:
        try:
            results["flow"] = service.update_flow()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def install_thread() -> None:
        try:
            results["install"] = controller.start("0.3.0", "0.3.0")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=status_thread, daemon=True),
        threading.Thread(target=install_thread, daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    stuck = [thread for thread in threads if thread.is_alive()]

    assert not stuck, "更新状态与安装启动形成 ABBA 死锁"
    assert not errors
    assert results.get("install", {}).get("status") == "starting"
    assert len(commands) == 1


def test_controller_start_reads_providers_without_holding_install_lock(tmp_path):
    """锁契约：调用 latest/artifact 提供方时安装锁必须空闲。

    必须用另一个线程判断：RLock 支持重入，同一线程 acquire(blocking=False) 永远成功。
    """
    bind: dict = {}
    seen: dict[str, bool] = {}

    def lock_free_elsewhere() -> bool:
        outcome: dict[str, bool] = {}

        def probe() -> None:
            # 换一个线程来取锁才能反映真实占用情况。
            acquired = bind["controller"]._lock.acquire(blocking=False)
            outcome["free"] = acquired
            if acquired:
                bind["controller"]._lock.release()

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(2)
        return bool(outcome.get("free"))

    def probe(name: str, value):
        seen[name] = lock_free_elsewhere()
        return value

    state = make_artifact(tmp_path)
    controller, commands, _ = make_controller(
        tmp_path, bind=bind,
        latest_provider=lambda: probe("latest", artifact_latest(state)),
        artifact_provider=lambda: probe("artifact", state),
    )

    assert controller.start("0.3.0", "0.3.0")["status"] == "starting"
    assert seen == {"latest": True, "artifact": True}
    assert len(commands) == 1


def test_controller_keeps_transaction_when_executor_still_alive(tmp_path):
    """P0：超时只代表疑似失联，执行器仍存活时绝不能解除安装互斥。"""
    stale = (datetime.now(timezone.utc)
             - timedelta(seconds=MAX_ACTIVE_SECONDS + 60)).isoformat(timespec="seconds")
    original = {"status": "installing", "target_version": "0.3.0", "pid": 4242,
                "message": "正在运行安装程序", "updated_at": stale}
    controller, commands, _ = make_controller(
        tmp_path, journal=original, pid_alive=lambda pid: True,
    )
    journal_path = tmp_path / "data" / "update-install.json"

    status = controller.status()

    assert status["status"] == "installing"
    assert status["active"] is True
    # 疑似失联只是查询时派生的提示，不能覆盖执行器写下的真实进度。
    assert status["stalled"] is True
    assert status["message"] == STALLED_MESSAGE
    assert json.loads(journal_path.read_text(encoding="utf-8")) == original
    with pytest.raises(ValueError, match="已有安装任务正在执行"):
        controller.start("0.3.0", "0.3.0")
    assert commands == []


def test_controller_reclaims_when_executor_exited(tmp_path):
    """执行器已退出（pid 不再存活）时，过期事务必须收尾并允许重新发起。"""
    stale = (datetime.now(timezone.utc)
             - timedelta(seconds=MAX_ACTIVE_SECONDS + 60)).isoformat(timespec="seconds")
    controller, commands, _ = make_controller(
        tmp_path,
        journal={"status": "installing", "target_version": "0.3.0",
                 "pid": 4242, "updated_at": stale},
        pid_alive=lambda pid: False,
    )

    status = controller.status()

    assert status["status"] == "failed"
    assert status["active"] is False
    assert controller.start("0.3.0", "0.3.0")["status"] == "starting"
    assert len(commands) == 1


def test_install_aborts_when_authorization_cancelled_during_verification(tmp_path):
    """P1：锁外 SHA-256 校验期间用户取消更新，安装不得继续启动。"""
    from menu_capture.updates.updates import UpdateService

    state = make_artifact(tmp_path)
    service = UpdateService(
        tmp_path / "runtime", role="worker", platform="windows", machine="AMD64",
    )
    service._schedule = {"armed": True, "version": "0.3.0", "mode": "idle",
                         "requested_at": "", "note": "", "last_result": ""}
    in_verify = threading.Event()
    proceed = threading.Event()

    def artifact_provider() -> dict:
        in_verify.set()
        assert proceed.wait(5)
        return completed_download_state(state)

    controller, commands, _ = make_controller(
        tmp_path,
        latest_provider=lambda: artifact_latest(state),
        artifact_provider=artifact_provider,
        authorization_check=service.claim_install,
        authorization_release=service.release_install_claim,
    )
    errors: list[BaseException] = []

    def install_thread() -> None:
        try:
            controller.start("0.3.0", "0.3.0", require_authorization=True)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=install_thread, daemon=True)
    thread.start()
    assert in_verify.wait(5)
    # 校验尚未结束时取消成功，随后安装必须因授权失效而退出。
    service.cancel_update()
    proceed.set()
    thread.join(10)

    assert not thread.is_alive()
    assert len(errors) == 1 and "授权已取消" in str(errors[0])
    assert commands == []


def test_cancel_after_authorization_claimed_is_rejected(tmp_path):
    """P1：授权被消费后取消必须失败，不能出现“取消成功但安装仍在执行”。"""
    from menu_capture.updates.updates import UpdateService

    state = make_artifact(tmp_path)
    service = UpdateService(
        tmp_path / "runtime", role="worker", platform="windows", machine="AMD64",
    )
    service._schedule = {"armed": True, "version": "0.3.0", "mode": "idle",
                         "requested_at": "", "note": "", "last_result": ""}
    controller, commands, _ = make_controller(
        tmp_path,
        artifact_provider=lambda: completed_download_state(state),
        authorization_check=service.claim_install,
        authorization_release=service.release_install_claim,
    )
    # 这里只验证“授权已消费”的线性化点，安装活动态由下一条并发用例覆盖。
    service.install_snapshot_provider = lambda: {"status": "idle"}

    controller.start("0.3.0", "0.3.0", require_authorization=True)

    with pytest.raises(ValueError, match="安装已开始"):
        service.cancel_update()
    with pytest.raises(ValueError, match="安装已开始"):
        service.cancel_install_schedule()
    assert len(commands) == 1


def test_install_and_cleanup_are_mutually_exclusive(tmp_path):
    """P2：清理持文件系统锁删除期间不允许启动安装，避免删掉正在读取的产物。"""
    fs_lock = threading.Lock()
    controller, commands, _ = make_controller(tmp_path, filesystem_lock=fs_lock)

    fs_lock.acquire()
    try:
        with pytest.raises(ValueError, match="存储清理进行中"):
            controller.start("0.3.0", "0.3.0")
    finally:
        fs_lock.release()
    assert commands == []

    # 释放后必须能正常启动，证明互斥不会把安装永久挡住。
    assert controller.start("0.3.0", "0.3.0")["status"] == "starting"


def test_active_nonblocking_is_read_only_and_never_waits(tmp_path):
    """事件循环线程的探测不取锁、不刷新、不写盘。"""
    controller, _, _ = make_controller(tmp_path)
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with controller._lock:
            held.set()
            release.wait(2)

    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(2)
    started = time.monotonic()
    try:
        assert controller.active_nonblocking() is False
    finally:
        elapsed = time.monotonic() - started
        release.set()
        thread.join(2)
    assert elapsed < 1.0
    # 只读探测不得创建或改写任务文件。
    assert not (tmp_path / "data" / "update-install.json").exists()


def test_concurrent_status_and_install_start_complete_without_deadlock(tmp_path):
    """并发回归：真实更新服务状态链路与安装启动反复交叉，线程必须在限期内退出。"""
    from menu_capture.updates.updates import UpdateService

    state = make_artifact(tmp_path)
    service = UpdateService(
        tmp_path / "runtime", role="worker", platform="windows", machine="AMD64",
    )
    service._cache = {"checked_at": time.time(), "latest": artifact_latest(state)}
    service._download_state = completed_download_state(state)
    controller, _, _ = make_controller(
        tmp_path,
        latest_provider=lambda: service.status_no_check().get("latest") or {},
        artifact_provider=service.download_status,
    )
    service.install_capability = lambda: True
    service.schedule_installer = lambda version: controller.start(version, version)
    service.install_snapshot_provider = controller.status
    journal = tmp_path / "data" / "update-install.json"

    stop = threading.Event()
    errors: list[BaseException] = []

    def status_loop() -> None:
        try:
            while not stop.is_set():
                service.update_flow()
                try:
                    service.cancel_update()
                except ValueError:
                    pass
                service.worker_software_status()
        except BaseException as exc:  # noqa: BLE001 - 线程内异常汇总到主线程断言
            errors.append(exc)

    def install_loop() -> None:
        try:
            while not stop.is_set():
                try:
                    controller.start("0.3.0", "0.3.0")
                except ValueError:
                    pass
                journal.unlink(missing_ok=True)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=status_loop), threading.Thread(target=install_loop)]
    for thread in threads:
        thread.start()
    # 先让两个线程充分交叉，再请求退出；真正死锁时线程无法观察到 stop。
    time.sleep(1.5)
    stop.set()
    for thread in threads:
        thread.join(timeout=3)
    stuck = [thread for thread in threads if thread.is_alive()]

    assert not stuck, "并发更新状态与安装启动发生死锁"
    assert not errors


# ---------- 增量补丁模式 ----------

import shutil  # noqa: E402

from menu_capture.updates import update_patch  # noqa: E402


def make_target_tree(root: Path, version: str = "0.3.0") -> Path:
    target = root / "target-tree"
    shutil.copytree(root / "releases" / "0.2.2", target)
    (target / "app/web/react/assets/index.js").unlink()
    (target / "app/web/react/assets/index-new.js").write_bytes(b"frontend")
    (target / "release-manifest.json").write_text(json.dumps({
        "version": version,
        "frontend_entry": "assets/index-new.js",
        "frontend_sha256": hashlib.sha256(b"frontend").hexdigest(),
    }), encoding="utf-8")
    return target


def make_patch_job(tmp_path: Path, root: Path, *, fallback_body: bytes | None = None) -> dict:
    target = make_target_tree(root)
    download_dir = tmp_path / "download"
    patch_path = download_dir / "0.3.0" / "MenuCapture-0.2.2-to-0.3.0.patch.zip"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    update_patch.make_patch(
        root / "releases" / "0.2.2", target,
        from_version="0.2.2", to_version="0.3.0", output=patch_path,
    )
    body = fallback_body if fallback_body is not None else b"installer-bytes"
    fallback = download_dir / "0.3.0" / "MenuCapture-v0.3.0.exe"
    fallback.write_bytes(body)
    return {
        "schema": 2,
        "asset_kind": "windows_patch",
        "patch_path": str(patch_path),
        "patch_asset_name": patch_path.name,
        "patch_sha256": update_patch.file_sha256(patch_path),
        "patch_size": patch_path.stat().st_size,
        "base_version": "0.2.2",
        "base_commit": "",
        "target_commit": "",
        "patch_catalog": "",
        "fallback_name": fallback.name,
        "fallback_sha256": hashlib.sha256(body).hexdigest(),
        "fallback_size": len(body),
        "fallback_url": f"https://github.com/x/y/releases/download/v0.3.0/{fallback.name}",
        "download_dir": str(download_dir),
        "data_dir": str(tmp_path / "data"),
        "target_version": "0.3.0",
        "previous_version": "0.2.2",
        "install_root": str(root),
        "port": 8765,
    }


def patch_orchestrator(tmp_path: Path, root: Path, job: dict, run_command, probe,
                       *, catalog=None, publisher=None):
    clock = FakeClock()

    def sleep(seconds: float) -> None:
        clock.now += seconds

    path = tmp_path / "data" / "update-install.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return InstallOrchestrator(
        job=job, journal_path=path, run_command=run_command, probe_version=probe,
        verify_publisher=publisher or (lambda artifact: {"status": "valid"}),
        verify_catalog=catalog or (lambda patch, cat: {"status": "valid"}),
        sleep=sleep, clock=clock, health_timeout=0.05,
    )


def make_patch_controller(tmp_path):
    from menu_capture.updates.updates import UpdateService

    state, latest = {}, {}
    controller, commands, root = make_controller(
        tmp_path, artifact_provider=lambda: state, latest_provider=lambda: latest,
    )
    job = make_patch_job(tmp_path, root)
    state.update({
        "status": "completed", "version": job["target_version"], "asset_kind": "windows_patch",
        "asset_name": job["patch_asset_name"], "arch": "x64", "sha256": job["patch_sha256"],
        "total": job["patch_size"], "path": job["patch_path"], "from_version": job["base_version"],
    })
    latest.update({
        "eligible": True, "version": job["target_version"], "tag": "v0.3.0",
        "patches": [{
            "name": job["patch_asset_name"], "from": job["base_version"], "platform": "windows",
            "arch": "x64", "sha256": job["patch_sha256"], "size": job["patch_size"],
        }],
        "assets": [{
            "kind": "windows_installer", "platform": "windows", "arch": "x64",
            "name": job["fallback_name"], "sha256": job["fallback_sha256"],
            "size": job["fallback_size"], "url": job["fallback_url"],
        }],
    })
    service = UpdateService(tmp_path / "runtime", role="worker", platform="windows", machine="AMD64")
    service._download_state = state
    service.download_dir = controller.download_dir
    service.install_capability = lambda: True
    service.schedule_installer = lambda version: controller.start(version, version, require_authorization=True)
    controller.authorization_check = service.claim_install
    return controller, service, commands, root, state


@pytest.mark.parametrize("entry", ["direct", "scheduled"])
@pytest.mark.parametrize("problem", ["missing", "size", "sha256", "baseline", "running-version"])
def test_patch_unavailable_after_download_falls_back_from_install_entry(tmp_path, monkeypatch, entry, problem):
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_INCREMENTAL_UPDATE", "1")
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", "1")
    monkeypatch.setattr("menu_capture.updates.updates.SCHEDULE_POLL_SECONDS", 0)
    controller, service, commands, root, state = make_patch_controller(tmp_path)
    path = Path(state["path"])
    if problem == "missing":
        path.unlink()
    elif problem == "size":
        path.write_bytes(b"wrong-size")
    elif problem == "sha256":
        path.write_bytes(b"x" * state["total"])
    elif problem == "baseline":
        (root / "releases/0.2.2/release-manifest.json").write_text('{"version":"0.2.1"}', encoding="utf-8")
    else:
        make_release(root, "0.2.3")
        (root / "current-version").write_text("0.2.3", encoding="utf-8")
        controller.current_version = lambda: "0.2.3"

    if entry == "scheduled":
        # 执行真实调度循环，省去定时等待；控制器仍要消费同一份授权才能启动。
        monkeypatch.setattr(service, "_arm_schedule_monitor", lambda: service._schedule_loop(threading.Event(), "0.3.0"))
        service.schedule_install()
        assert service.install_schedule()["last_result"] == "started"
        assert not service.claim_install("0.3.0")
    else:
        controller.start("0.3.0", "0.3.0")

    assert len(commands) == 1
    job = json.loads(controller.job_path.read_text(encoding="utf-8"))
    assert job["asset_kind"] == "windows_patch"
    installed, verified = [], []
    downloads = []

    @contextmanager
    def open_fallback(url, headers, timeout):
        downloads.append(url)
        yield 200, {}, io.BytesIO(b"installer-bytes")

    if problem == "missing":
        (controller.download_dir / "0.3.0" / job["fallback_name"]).unlink()
    monkeypatch.setattr("menu_capture.updates.update_install._http_open", open_fallback)

    def run(args, timeout):
        installed.append(Path(args[0]).name)
        make_release(root, "0.3.0")
        (root / "current-version").write_text("0.3.0", encoding="utf-8")
        return 0, ""

    def verify_publisher(path):
        verified.append(path.name)
        return {"status": "valid"}

    orchestrator = patch_orchestrator(tmp_path, root, job, run, lambda port: "0.3.0", publisher=verify_publisher)
    assert orchestrator.execute()["status"] == "completed"
    assert installed == verified == [job["fallback_name"]]
    assert downloads == ([job["fallback_url"]] if problem == "missing" else [])


@pytest.mark.parametrize("field,value,message", [
    ("path", "/elsewhere/patch.zip", "路径与版本目录不一致"),
    ("sha256", "f" * 64, "补丁身份与发布清单不一致"),
    ("from_version", "0.2.1", "增量补丁与当前发布清单不一致"),
])
def test_patch_install_rejects_untrusted_download_record(tmp_path, field, value, message):
    controller, _, commands, _, state = make_patch_controller(tmp_path)
    state[field] = value
    with pytest.raises(ValueError, match=message):
        controller.start("0.3.0", "0.3.0")
    assert commands == []


def test_patch_mode_rebuilds_and_registers(tmp_path, monkeypatch):
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_INCREMENTAL_UPDATE", "1")
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", "1")
    root = make_install_root(tmp_path)
    job = make_patch_job(tmp_path, root)

    def run(args, timeout):
        assert "-File" in args and "Launch.ps1" in " ".join(args)
        (root / "current-version").write_text("0.3.0", encoding="utf-8")
        return 0, ""

    orchestrator = patch_orchestrator(tmp_path, root, job, run, lambda port: "0.3.0")
    journal = orchestrator.execute()

    assert journal["status"] == "completed"
    target = root / "releases" / "0.3.0"
    assert (target / "app/web/react/assets/index-new.js").is_file()
    assert not (target / "app/web/react/assets/index.js").exists()
    assert not (root / "releases" / ".0.3.0.patch-tmp").exists()


def test_patch_requires_opt_in_and_falls_back_to_full(tmp_path, monkeypatch):
    # 未启用增量：补丁被拒绝，改用同一授权内的完整安装包。
    monkeypatch.delenv("MENU_CAPTURE_ALLOW_INCREMENTAL_UPDATE", raising=False)
    root = make_install_root(tmp_path)
    job = make_patch_job(tmp_path, root)
    installed: list[str] = []

    def run(args, timeout):
        installed.append(args[0])
        make_release(root, "0.3.0")
        (root / "current-version").write_text("0.3.0", encoding="utf-8")
        return 0, ""

    orchestrator = patch_orchestrator(tmp_path, root, job, run, lambda port: "0.3.0")
    journal = orchestrator.execute()

    assert journal["status"] == "completed"
    assert installed and installed[0].endswith("MenuCapture-v0.3.0.exe")


def test_patch_baseline_mismatch_falls_back_and_cleans_target(tmp_path, monkeypatch):
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_INCREMENTAL_UPDATE", "1")
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", "1")
    root = make_install_root(tmp_path)
    job = make_patch_job(tmp_path, root)
    # 补丁生成后基线被篡改：必须拒绝增量并回退全量。
    (root / "releases/0.2.2/app/menu_capture/api/server.py").write_text("tampered", encoding="utf-8")

    def run(args, timeout):
        make_release(root, "0.3.0")
        (root / "current-version").write_text("0.3.0", encoding="utf-8")
        return 0, ""

    orchestrator = patch_orchestrator(tmp_path, root, job, run, lambda port: "0.3.0")
    journal = orchestrator.execute()

    assert journal["status"] == "completed"
    assert (root / "releases/0.3.0/release-manifest.json").is_file()
    assert not (root / "releases" / ".0.3.0.patch-tmp").exists()


def test_patch_rejects_existing_target_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_INCREMENTAL_UPDATE", "1")
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", "1")
    root = make_install_root(tmp_path)
    job = make_patch_job(tmp_path, root)
    make_release(root, "0.3.0")
    calls: list[list[str]] = []

    def run(args, timeout):
        calls.append(args)
        return 0, ""

    orchestrator = patch_orchestrator(tmp_path, root, job, run, lambda port: "0.2.2")
    journal = orchestrator.execute()

    assert journal["status"] == "failed"
    assert "目标版本目录已存在" in journal["error"]
    assert calls == []


def test_patch_failure_without_fallback_reports_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_INCREMENTAL_UPDATE", "1")
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", "1")
    root = make_install_root(tmp_path)
    job = make_patch_job(tmp_path, root)
    # 基线在补丁生成后被篡改，补丁必然应用失败；同时移走完整安装包且不提供网络，
    # 回退不可用时应如实失败，而不是留下残缺目标目录。
    (root / "releases/0.2.2/app/menu_capture/api/server.py").write_text("tampered", encoding="utf-8")
    Path(job["download_dir"], "0.3.0", job["fallback_name"]).unlink()
    job["fallback_url"] = ""

    orchestrator = patch_orchestrator(tmp_path, root, job, lambda args, timeout: (0, ""), lambda port: "0.2.2")
    journal = orchestrator.execute()

    assert journal["status"] == "failed"
    assert "完整安装包" in journal["error"]
    assert not (root / "releases" / "0.3.0").exists()


@pytest.mark.parametrize("field", ["patch_size", "patch_sha256"])
def test_patch_identity_mismatch_falls_back_to_full_once(tmp_path, monkeypatch, field):
    """补丁身份不符只说明增量不可用：同一次授权内回退一次完整安装包，不重复安装。"""
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_INCREMENTAL_UPDATE", "1")
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", "1")
    root = make_install_root(tmp_path)
    job = make_patch_job(tmp_path, root)
    if field == "patch_size":
        job[field] += 1
    else:
        job[field] = "0" * 64
    installs: list[str] = []

    def run(args, timeout):
        installs.append(args[0])
        make_release(root, "0.3.0")
        (root / "current-version").write_text("0.3.0", encoding="utf-8")
        return 0, ""

    orchestrator = patch_orchestrator(tmp_path, root, job, run, lambda port: "0.3.0")
    journal = orchestrator.execute()

    assert journal["status"] == "completed"
    assert len(installs) == 1
    assert installs[0].endswith("MenuCapture-v0.3.0.exe")
    assert not (root / "releases" / ".0.3.0.patch-tmp").exists()


@pytest.mark.parametrize("field,value", [
    ("base_version", "0.2.1"),
    ("base_commit", "b" * 40),
])
def test_patch_baseline_identity_mismatch_falls_back_to_full(tmp_path, monkeypatch, field, value):
    """基线版本/提交与本地实际基线不一致时，补丁不可用但不影响完整安装。"""
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_INCREMENTAL_UPDATE", "1")
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", "1")
    root = make_install_root(tmp_path)
    job = make_patch_job(tmp_path, root)
    job[field] = value
    installs: list[str] = []

    def run(args, timeout):
        installs.append(args[0])
        make_release(root, "0.3.0")
        (root / "current-version").write_text("0.3.0", encoding="utf-8")
        return 0, ""

    orchestrator = patch_orchestrator(tmp_path, root, job, run, lambda port: "0.3.0")
    journal = orchestrator.execute()

    assert journal["status"] == "completed"
    assert len(installs) == 1


def test_patch_leftover_stage_is_never_deleted(tmp_path, monkeypatch):
    """无法确认归属的暂存目录必须保留现场，只允许回退完整安装。"""
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_INCREMENTAL_UPDATE", "1")
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", "1")
    root = make_install_root(tmp_path)
    job = make_patch_job(tmp_path, root)
    stage = root / "releases" / ".0.3.0.patch-tmp"
    stage.mkdir()
    sentinel = stage / "foreign.txt"
    sentinel.write_text("keep", encoding="utf-8")

    def run(args, timeout):
        make_release(root, "0.3.0")
        (root / "current-version").write_text("0.3.0", encoding="utf-8")
        return 0, ""

    orchestrator = patch_orchestrator(tmp_path, root, job, run, lambda port: "0.3.0")
    journal = orchestrator.execute()

    assert journal["status"] == "completed"
    assert sentinel.is_file()


def test_cleanup_target_preserves_registered_version(tmp_path):
    """运行指针仍指向目标目录时，即使目录由本次事务创建也不能删除。"""
    root = make_install_root(tmp_path)
    job = make_patch_job(tmp_path, root)
    orchestrator = patch_orchestrator(
        tmp_path, root, job, lambda args, timeout: (0, ""), lambda port: "0.3.0"
    )
    make_release(root, "0.3.0")
    (root / "current-version").write_text("0.3.0", encoding="utf-8")
    orchestrator._target_created = True

    orchestrator._cleanup_target_best_effort()

    assert (root / "releases" / "0.3.0").is_dir()
    assert orchestrator._target_created is True


def test_cleanup_target_removes_only_owned_unregistered_version(tmp_path):
    """只清理本次事务创建、且未登记的目标目录，既有版本不受影响。"""
    root = make_install_root(tmp_path)
    job = make_patch_job(tmp_path, root)
    orchestrator = patch_orchestrator(
        tmp_path, root, job, lambda args, timeout: (0, ""), lambda port: "0.2.2"
    )
    make_release(root, "0.3.0")
    orchestrator._target_created = True

    orchestrator._cleanup_target_best_effort()

    assert not (root / "releases" / "0.3.0").exists()
    assert (root / "releases" / "0.2.2").is_dir()
    assert orchestrator._target_created is False


def test_full_install_failure_cleans_transaction_target(tmp_path):
    """安装器失败但未切换版本指针时，必须清掉残缺目标目录，避免阻塞下次安装。"""
    root = make_install_root(tmp_path)

    def run(args, timeout):
        make_release(root, "0.3.0")
        return 1, "setup failed"

    orchestrator, _, _ = make_orchestrator(tmp_path, run_command=run, probe=lambda port: "0.2.2")
    journal = orchestrator.execute()

    assert journal["status"] == "failed"
    assert not (root / "releases" / "0.3.0").exists()
    assert (root / "releases" / "0.2.2").is_dir()
    assert (root / "current-version").read_text(encoding="utf-8") == "0.2.2"


def test_schema_one_full_job_still_supported(tmp_path):
    root = make_install_root(tmp_path)
    state = make_artifact(tmp_path)
    job = {
        "schema": 1,
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

    def run(args, timeout):
        make_release(root, "0.3.0")
        (root / "current-version").write_text("0.3.0", encoding="utf-8")
        return 0, ""

    path = tmp_path / "data" / "update-install.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    orchestrator = InstallOrchestrator(
        job=job, journal_path=path, run_command=run, probe_version=lambda port: "0.3.0",
        verify_publisher=lambda artifact: {"status": "valid"},
        sleep=lambda seconds: None, clock=clock, health_timeout=0.05,
    )

    assert orchestrator.execute()["status"] == "completed"
