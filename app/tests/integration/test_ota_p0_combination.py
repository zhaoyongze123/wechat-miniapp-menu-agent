"""P0/P1 真实模块组合回归测试（macOS 与 Windows 均可运行）。

覆盖：
- P0-1: MenuCaptureLauncher._verify_home_path -> 真实 recovery.prepare_verify_home + 真实 SQLite WAL 副本
- P0-2: InstallTransaction 与 update-watchdog.ps1 共用 update-install.lock、stopping 恢复与 Windows 字节锁互斥
- P0-3: VelopackBackend.prepare_recovery_package / rollback 跨渠道（stable->preview、preview->stable、同渠道）
- P0-4: 真实 InstallTransaction + MenuCaptureLauncher._handle_prepared 预检失败落盘与 shutdown 响应丢失 fail-closed
- P1-6: 真实 create_app 在 verifying 阶段阻断后台业务/同步服务，仅在 completed 后经内部接口放行
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from menu_capture.api.server import create_app
from menu_capture.updates import recovery
from menu_capture.updates.install_transaction import InstallTransaction
from menu_capture.updates.velopack_backend import VelopackBackend, VelopackError

REPO_ROOT = Path(__file__).resolve().parents[3]
LAUNCHER_PATH = REPO_ROOT / "deploy" / "windows" / "MenuCaptureLauncher.py"
WATCHDOG_PATH = REPO_ROOT / "deploy" / "windows" / "update-watchdog.ps1"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("menu_capture_launcher_under_test", LAUNCHER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_real_data_root(root: Path) -> Path:
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    db_path = data / "tasks.sqlite3"
    with closing(sqlite3.connect(db_path)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(
            """
            CREATE TABLE tasks(id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE runs(id TEXT PRIMARY KEY, status TEXT NOT NULL);
            CREATE INDEX idx_tasks_name ON tasks(name);
            INSERT INTO tasks VALUES ('s1', '门店甲'), ('s2', '门店乙');
            INSERT INTO runs VALUES ('r1', 'completed');
            """
        )
        db.commit()
    (root / "settings.json").write_text('{"mode":"worker"}', encoding="utf-8")
    return data


def _write_release_dir(current: Path, *, version: str, commit: str) -> None:
    # 与 deploy/windows/build.ps1 的实际打包结构一致：current/app/web/react/...
    frontend = current / "app" / "web" / "react" / "assets" / "index-test.js"
    frontend.parent.mkdir(parents=True, exist_ok=True)
    body = f"// {version}-{commit[:8]}\n".encode("utf-8")
    frontend.write_bytes(body)
    manifest = {
        "version": version,
        "git_commit": commit,
        "frontend_entry": "assets/index-test.js",
        "frontend_sha256": hashlib.sha256(body).hexdigest(),
    }
    (current / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _make_nupkg_bytes(*, version: str, commit: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(
            "lib/net45/release-manifest.json",
            json.dumps({
                "version": version,
                "git_commit": commit,
                "frontend_entry": "index.html",
                "frontend_sha256": "e" * 64,
            }),
        )
    return buf.getvalue()


def _make_release_manifest(
    *,
    repo: str,
    version: str,
    commit: str,
    channel: str,
    full_name: str,
    full_bytes: bytes,
) -> dict:
    tag = f"v{version}"
    full_sha = hashlib.sha256(full_bytes).hexdigest()

    def asset(name: str, raw: bytes) -> dict:
        return {
            "name": name,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "url": f"https://github.com/{repo}/releases/download/{tag}/{name}",
        }

    return {
        "version": version,
        "channel": channel,
        "git_commit": commit,
        "min_supported_version": "0.3.0",
        "rollback": "supported",
        "assets": [
            {
                "kind": "hub_image",
                "name": f"hub-image-{version}.tar.gz",
                "size": 1024,
                "sha256": "f" * 64,
                "url": f"https://github.com/{repo}/releases/download/{tag}/hub-image-{version}.tar.gz",
                "platform": "linux",
                "arch": "amd64",
            },
        ],
        "velopack": {
            "schema_version": 1,
            "pack_id": "Yipu.MenuCapture",
            "platform": "windows",
            "arch": "x64",
            "channel": f"win-x64-{channel}",
            "version": version,
            "git_commit": commit,
            "tag": tag,
            "setup": asset(f"MenuCapture-{version}-Setup.exe", b"setup"),
            "full": {
                "name": full_name,
                "size": len(full_bytes),
                "sha256": full_sha,
                "url": f"https://github.com/{repo}/releases/download/{tag}/{full_name}",
            },
            "deltas": [],
            "index": asset(f"releases.win-x64-{channel}.json", b"{}"),
            "trust": {"mode": "unsigned"},
        },
    }


def _create_prepared_tx(
    data_dir: Path,
    *,
    previous_version: str = "0.3.4",
    previous_commit: str = "a" * 40,
    target_version: str = "0.4.0",
    target_commit: str = "b" * 40,
    channel: str = "stable",
    recovery_channel: str = "stable",
) -> tuple[InstallTransaction, dict]:
    tx = InstallTransaction(data_dir)
    snapshot = recovery.snapshot_database(data_dir, data_dir / "recovery" / "baseline.sqlite3")
    row = tx.create(
        previous_version=previous_version,
        previous_commit=previous_commit,
        target_version=target_version,
        target_commit=target_commit,
        channel=channel,
        schema_baseline={
            "version": previous_version,
            "schema": snapshot["schema"],
            "snapshot": snapshot,
        },
        target_asset={
            "version": target_version,
            "git_commit": target_commit,
            "channel": f"win-x64-{channel}",
            "full": {"name": f"Yipu.MenuCapture-{target_version}-full.nupkg"},
            "trust": {"mode": "unsigned"},
        },
        recovery_asset={
            "version": previous_version,
            "commit": previous_commit,
            "channel": recovery_channel,
            "name": f"Yipu.MenuCapture-{previous_version}-full.nupkg",
            "size": 16,
            "sha256": "c" * 64,
            "path": str(data_dir / "recovery" / f"Yipu.MenuCapture-{previous_version}-full.nupkg"),
            "source": {"type": "official-release", "url": "https://example.invalid/old.nupkg"},
            "inner_manifest": {"version": previous_version, "commit": previous_commit},
        },
    )
    return tx, row


# ---------- P0-1: Launcher 隔离验证路径与真实 prepare_verify_home 组合 ----------


def test_p0_1_launcher_verify_home_works_with_real_prepare_verify_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    install_root = tmp_path / "Yipu.MenuCapture"
    current = install_root / "current"
    (install_root / "packages").mkdir(parents=True)
    _write_release_dir(current, version="0.4.0", commit="b" * 40)
    data_root = tmp_path / "MenuCapture"
    _create_real_data_root(data_root)

    monkeypatch.setattr(launcher, "_install_root", lambda: install_root)
    monkeypatch.setattr(launcher, "_current_dir", lambda: current)
    monkeypatch.delenv("MENU_CAPTURE_VERIFY_ROOT", raising=False)

    verify_home = launcher._verify_home_path(data_root, "tx-real-001")
    assert not verify_home.resolve().is_relative_to(data_root.resolve())
    assert not verify_home.resolve().is_relative_to(install_root.resolve())

    evidence = recovery.prepare_verify_home(
        data_root,
        verify_home,
        transaction_id="tx-real-001",
        forbidden_roots=(install_root, current, install_root / "packages"),
    )
    assert "tasks.sqlite3" in evidence["databases"]
    readable, reason = recovery.database_readable(verify_home / "data")
    assert readable is True, reason


def test_p0_1_verify_candidate_end_to_end_with_real_transaction_and_prepare_verify_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    install_root = tmp_path / "Yipu.MenuCapture"
    current = install_root / "current"
    (install_root / "packages").mkdir(parents=True)
    _write_release_dir(current, version="0.4.0", commit="b" * 40)
    data_root = tmp_path / "MenuCapture"
    data_dir = _create_real_data_root(data_root)
    tx, row = _create_prepared_tx(data_dir)
    owner = "launcher-owner-1"
    tx.claim(row["transaction_id"], "prepared", owner_token=owner,
             pid=os.getpid(), process_started_at=recovery.current_process_created_at(), phase="verifying")
    for before, after in (("prepared", "stopping"), ("stopping", "applying"), ("applying", "verifying")):
        tx.transition(row["transaction_id"], before, after, owner_token=owner)

    monkeypatch.setattr(launcher, "_install_root", lambda: install_root)
    monkeypatch.setattr(launcher, "_current_dir", lambda: current)
    monkeypatch.delenv("MENU_CAPTURE_VERIFY_ROOT", raising=False)

    started_homes: list[tuple[Path | None, bool]] = []
    released_calls: list[tuple[str, str]] = []

    class FakeWorker:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def poll(self):
            return None

        def close(self) -> None:
            return None

    def fake_start_worker(_data_root: Path, _token: str, *, home: Path | None = None,
                          verify: bool = False, stop_nonce: str = ""):
        started_homes.append((home, verify))
        return FakeWorker(1001 if verify else 1002)

    monkeypatch.setattr(launcher, "_start_worker", fake_start_worker)
    monkeypatch.setattr(launcher, "_wait_worker_health", lambda *_a, **_k: (True, ""))
    monkeypatch.setattr(launcher, "_wait_worker_exit", lambda *_a, **_k: (True, ""))
    monkeypatch.setattr(recovery, "request_verify_exit", lambda *_a, **_k: True)
    monkeypatch.setattr(
        recovery,
        "request_release_business_gate",
        lambda _port, _token, *, transaction_id, target_version: (
            released_calls.append((transaction_id, target_version)) or True
        ),
    )

    ctx = launcher.Context(data_root, "tok", backend=SimpleNamespace(), transaction=tx, owner_token=owner)
    outcome = launcher._verify_candidate(ctx, tx.read())
    assert outcome == "running"
    final = tx.read()
    assert final["status"] == "completed"
    assert final["evidence"]["verify_copy_ok"] is True
    assert final["evidence"]["real_start_ok"] is True
    assert len(started_homes) == 2
    verify_home_used, is_verify = started_homes[0]
    assert is_verify is True and verify_home_used is not None
    assert not verify_home_used.resolve().is_relative_to(data_root.resolve())
    assert released_calls == [(row["transaction_id"], "0.4.0")]


# ---------- P0-3: 恢复包跨渠道（stable->preview / preview->stable / 同渠道） ----------


@pytest.mark.parametrize(
    ("prev_ver", "prev_channel", "target_ver", "target_channel"),
    [
        ("0.3.4", "stable", "0.4.0-rc.1", "preview"),
        ("0.4.0-rc.1", "preview", "0.4.0", "stable"),
        ("0.3.4", "stable", "0.4.0", "stable"),
    ],
)
def test_p0_3_prepare_recovery_package_and_rollback_use_previous_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prev_ver: str,
    prev_channel: str,
    target_ver: str,
    target_channel: str,
) -> None:
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", "1")
    repo = "zhaoyongze123/wechat-miniapp-menu-agent"
    prev_commit = "a" * 40
    full_name = f"Yipu.MenuCapture-{prev_ver}-full.nupkg"
    full_bytes = _make_nupkg_bytes(version=prev_ver, commit=prev_commit)
    manifest = _make_release_manifest(
        repo=repo,
        version=prev_ver,
        commit=prev_commit,
        channel=prev_channel,
        full_name=full_name,
        full_bytes=full_bytes,
    )

    install_root = tmp_path / "Yipu.MenuCapture"
    packages = install_root / "packages"
    packages.mkdir(parents=True)
    (packages / full_name).write_bytes(full_bytes)
    monkeypatch.setenv("MENU_CAPTURE_VELOPACK_PACKAGES", str(packages))

    backend = VelopackBackend(repo, tmp_path / "data")
    fetched_channels: list[tuple[str, str]] = []

    def fake_fetch(version: str, channel: str) -> dict:
        fetched_channels.append((version, channel))
        assert version == prev_ver
        assert channel == prev_channel
        return manifest

    monkeypatch.setattr(backend, "fetch_release_manifest", fake_fetch)

    target = {
        "version": target_ver,
        "git_commit": "b" * 40,
        "channel": f"win-x64-{target_channel}",
        "trust": {"mode": "unsigned"},
    }
    recovery_asset = backend.prepare_recovery_package(prev_ver, prev_commit, target)
    assert fetched_channels == [(prev_ver, prev_channel)]
    assert recovery_asset["channel"] == prev_channel
    assert recovery_asset["version"] == prev_ver
    assert Path(recovery_asset["path"]).is_file()

    # 验证 rollback 使用旧包实际渠道，且传入目标渠道不一致时 fail-closed 拒绝。
    manager_channels: list[str] = []

    class FakeManager:
        def wait_exit_then_apply_updates(self, old_asset, *, silent, restart, restart_args):
            assert old_asset.Version == prev_ver
            assert old_asset.FileName == full_name
            assert silent is True and restart is True and restart_args is None

    monkeypatch.setattr(backend, "available", lambda: True)
    monkeypatch.setattr(
        backend,
        "manager",
        lambda ch=None, version=None: (manager_channels.append(str(ch)) or FakeManager()),
    )

    fake_velopack = SimpleNamespace(VelopackAsset=lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setitem(sys.modules, "velopack", fake_velopack)

    if prev_channel != target_channel:
        with pytest.raises(VelopackError, match="回滚渠道与恢复包实际发布渠道不一致"):
            backend.rollback(recovery_asset, channel=target_channel)
        assert manager_channels == []

    backend.rollback(recovery_asset, channel=prev_channel)
    assert manager_channels == [prev_channel]


# ---------- P0-4: prepared 预检失败合法落盘 & HTTP shutdown 响应丢失 fail-closed ----------


def test_p0_4_prepared_precheck_failures_claim_first_and_record_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    data_root = tmp_path / "MenuCapture"
    data_dir = _create_real_data_root(data_root)
    current = tmp_path / "Yipu.MenuCapture" / "current"

    class FakeWorker:
        pid = 2001

        def poll(self):
            return None

    # 场景 1：运行版本与 previous_version 不一致 -> 必须合法落盘为 failed，不抛 TransactionError。
    _write_release_dir(current, version="0.3.3", commit="a" * 40)
    tx, _row = _create_prepared_tx(data_dir)
    monkeypatch.setattr(launcher, "_current_dir", lambda: current)
    ctx = launcher.Context(data_root, "tok", backend=SimpleNamespace(), transaction=tx, owner_token="owner-a")
    assert launcher._handle_prepared(ctx, FakeWorker()) == "keep"
    recorded = tx.read()
    assert recorded["status"] == "failed"
    assert recorded["owner_token"] == "owner-a"
    assert "前置版本不一致" in recorded["error"]

    # 场景 2：verify_pending 资产校验失败 -> 同样合法落盘为 failed 并保留旧 Worker。
    _write_release_dir(current, version="0.3.4", commit="a" * 40)
    tx2, _row2 = _create_prepared_tx(data_dir)

    class FailingBackend:
        def verify_pending(self, _target, _channel):
            raise ValueError("完整包 SHA-256 不一致")

        def verify_recovery_package(self, _asset):
            raise AssertionError("should not reach")

    ctx2 = launcher.Context(data_root, "tok", backend=FailingBackend(), transaction=tx2, owner_token="owner-b")
    assert launcher._handle_prepared(ctx2, FakeWorker()) == "keep"
    recorded2 = tx2.read()
    assert recorded2["status"] == "failed"
    assert recorded2["owner_token"] == "owner-b"
    assert "SHA-256 不一致" in recorded2["error"]


def test_p0_4_http_shutdown_transport_error_never_unlocks_to_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    data_root = tmp_path / "MenuCapture"
    data_dir = _create_real_data_root(data_root)
    current = tmp_path / "Yipu.MenuCapture" / "current"
    _write_release_dir(current, version="0.3.4", commit="a" * 40)
    tx, _row = _create_prepared_tx(data_dir)

    class OkBackend:
        def verify_pending(self, _target, _channel):
            return {"version": "0.4.0"}

        def verify_recovery_package(self, _asset):
            return Path("dummy")

    class StubbornWorker:
        pid = 3001

        def poll(self):
            return None

    monkeypatch.setattr(launcher, "_current_dir", lambda: current)
    monkeypatch.setattr(launcher, "_start_watchdog", lambda *_a, **_k: True)
    monkeypatch.setattr(
        recovery,
        "request_worker_shutdown_result",
        lambda *_a, **_k: ("transport_error", "RemoteDisconnected"),
    )
    monkeypatch.setattr(launcher, "_wait_worker_exit", lambda *_a, **_k: (False, "Worker 未在超时内退出"))

    ctx = launcher.Context(data_root, "tok", backend=OkBackend(), transaction=tx, owner_token="owner-c")
    outcome = launcher._handle_prepared(ctx, StubbornWorker())
    assert outcome == "recovery"
    recorded = tx.read()
    assert recorded["status"] == "recovery_required"
    assert "关闭响应丢失" in recorded["error"]


# ---------- P0-2: 看门狗共用锁与真实 PowerShell 执行测试 ----------


def test_p0_2_lock_path_matches_between_python_and_watchdog(tmp_path: Path) -> None:
    tx = InstallTransaction(tmp_path)
    assert tx.lock_path.name == "update-install.lock"
    script_text = WATCHDOG_PATH.read_text(encoding="utf-8")
    assert "ChangeExtension($journalPath, '.lock')" in script_text
    assert '"$journalPath.lock"' not in script_text


def _powershell_exe() -> str | None:
    if sys.platform == "win32":
        return shutil.which("powershell.exe") or shutil.which("pwsh")
    return shutil.which("pwsh")


@pytest.mark.skipif(_powershell_exe() is None, reason="需要 PowerShell 执行真实 update-watchdog.ps1")
def test_p0_2_real_watchdog_script_handles_stopping_and_shared_lock(tmp_path: Path) -> None:
    ps = _powershell_exe()
    assert ps is not None
    data_root = tmp_path / "MenuCapture"
    data_dir = _create_real_data_root(data_root)
    install_root = tmp_path / "Yipu.MenuCapture"
    install_root.mkdir(parents=True)
    tx, row = _create_prepared_tx(data_dir)
    tid = row["transaction_id"]

    # 1. 协调器已退出且处于 stopping 超时 -> 真实 PS 看门狗必须落盘 recovery_required。
    tx.claim(tid, "prepared", owner_token="dead-owner", pid=999999, process_started_at=1000.0, phase="stopping")
    tx.transition(tid, "prepared", "stopping", owner_token="dead-owner")
    raw = tx.read()
    raw["updated_at"] = "2020-01-01T00:00:00+00:00"
    tx.path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    recovery_dir = data_root / "recovery"
    recovery_dir.mkdir(parents=True, exist_ok=True)
    control_path = recovery_dir / "watchdog-control.json"
    control_path.write_text(
        json.dumps({
            "mode": "apply",
            "transaction_id": tid,
            "owner_token": "dead-owner",
            "watchdog_nonce": "nonce-1",
            "target_version": "0.4.0",
            "target_commit": "b" * 40,
            "previous_version": "0.3.4",
            "previous_commit": "a" * 40,
            "channel": "stable",
            "install_root": str(install_root),
            "data_root": str(data_root),
            "port": 8765,
            "launcher_pid": 999999,
            "stale_after_seconds": 1,
        }),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(WATCHDOG_PATH), "-ControlFile", str(control_path), "-Once"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert completed.returncode == 0, f"stdout={completed.stdout}\nstderr={completed.stderr}"
    after = tx.read()
    assert after["status"] == "recovery_required"
    assert after["evidence"]["watchdog_observed_state"] == "dead"
    assert after["evidence"]["watchdog_updater_state"] == "stopped"
    assert after["evidence"]["failed_owner_token"] == "dead-owner"
    assert (recovery_dir / "watchdog-ready.json").is_file()

    # 2. Windows 真实字节锁互斥：Python InstallTransaction._lock() 与 PS FileStream.Lock(0, 1)。
    if os.name == "nt":
        probe_cmd = (
            "$s = [IO.File]::Open($env:LOCK_TEST_PATH, [IO.FileMode]::OpenOrCreate, "
            "[IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite); "
            "try { $s.Lock(0, 1); $s.Unlock(0, 1); Write-Output 'LOCKED_OK' } "
            "catch [IO.IOException] { Write-Output 'LOCK_CONTENDED'; exit 42 } "
            "finally { $s.Dispose() }"
        )
        probe_env = {**os.environ, "LOCK_TEST_PATH": str(tx.lock_path)}
        with tx._lock():
            contended = subprocess.run(
                [ps, "-NoProfile", "-NonInteractive", "-Command", probe_cmd],
                capture_output=True, text=True, timeout=15, check=False, env=probe_env,
            )
            assert contended.returncode == 42, contended.stdout + contended.stderr
            assert "LOCK_CONTENDED" in contended.stdout

        free_run = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-Command", probe_cmd],
            capture_output=True, text=True, timeout=15, check=False, env=probe_env,
        )
        assert free_run.returncode == 0, free_run.stdout + free_run.stderr
        assert "LOCKED_OK" in free_run.stdout


# ---------- P1-6: verifying 阶段阻断后台业务/同步服务，completed 后才放行 ----------


def test_p1_6_verifying_transaction_blocks_background_services_until_release_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from menu_capture.api import server as server_mod

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    # 使用真实 TaskStore 初始化完整业务表结构。
    server_mod.TaskStore(data_dir / "tasks.sqlite3")
    tx, row = _create_prepared_tx(data_dir)
    tid = row["transaction_id"]
    owner = "owner-v"
    tx.claim(tid, "prepared", owner_token=owner, pid=os.getpid(),
             process_started_at=recovery.current_process_created_at(), phase="verifying")
    for before, after in (("prepared", "stopping"), ("stopping", "applying"), ("applying", "verifying")):
        tx.transition(tid, before, after, owner_token=owner)

    token = "coord-secret-token"
    monkeypatch.setenv("MENU_CAPTURE_COORDINATOR_TOKEN", token)

    called: list[str] = []

    monkeypatch.setattr(server_mod.UiPathRunner, "recover_publishes", lambda self: called.append("recover_publishes"))
    monkeypatch.setattr(server_mod.TaskStore, "recover", lambda self: called.append("store.recover"))
    monkeypatch.setattr(server_mod.MaterialService, "start_job_runner", lambda self: called.append("materials.start"))
    monkeypatch.setattr(server_mod.SharedWorker, "start", lambda self: called.append("worker.start"))

    app = create_app(tmp_path, role="worker")
    assert app.state.business_gate_active is True
    assert called == []

    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as client:
        # verifying 期间 lifespan 不得启动任何后台业务/同步服务。
        assert called == []
        assert client.get("/healthz").status_code == 200

        # 事务尚未 completed 时，放行请求必须被 409 拒绝。
        denied = client.post(
            "/api/internal/release-business-gate",
            json={"transaction_id": tid, "target_version": "0.4.0"},
            headers={"X-Coordinator-Token": token},
        )
        assert denied.status_code == 409
        assert called == []

        # 事务合法进入 completed 后，放行接口才一次性激活后台业务与同步服务。
        tx.transition(tid, "verifying", "completed", owner_token=owner)
        released = client.post(
            "/api/internal/release-business-gate",
            json={"transaction_id": tid, "target_version": "0.4.0"},
            headers={"X-Coordinator-Token": token},
        )
        assert released.status_code == 200
        assert released.json() == {"ok": True, "business_started": True}
        assert app.state.business_gate_active is False
        assert called == ["recover_publishes", "store.recover", "materials.start", "worker.start"]


def test_verify_release_uses_real_velopack_app_web_layout(tmp_path: Path) -> None:
    """verify_release 必须校验真实安装结构 current/app/web/react，而非缺失 app/ 的假想路径。"""
    current = tmp_path / "current"
    _write_release_dir(current, version="0.5.5", commit="c" * 40)

    # 1. 真实打包结构 current/app/web/react 通过校验
    manifest = recovery.verify_release(current, "0.5.5", "c" * 40)
    assert manifest["frontend_entry"] == "assets/index-test.js"

    # 2. 篡改前端入口文件内容必须拒绝
    target = current / "app" / "web" / "react" / "assets" / "index-test.js"
    target.write_bytes(b"// tampered\n")
    with pytest.raises(ValueError, match="前端入口文件哈希与发布清单不一致"):
        recovery.verify_release(current, "0.5.5", "c" * 40)

    # 3. 缺失 app/web/react 时即使存在旧假想路径 current/web/react 也必须报缺失
    target.unlink()
    wrong_layout = current / "web" / "react" / "assets" / "index-test.js"
    wrong_layout.parent.mkdir(parents=True, exist_ok=True)
    wrong_layout.write_bytes(f"// 0.5.5-{'c' * 8}\n".encode("utf-8"))
    with pytest.raises(ValueError, match="前端入口文件缺失"):
        recovery.verify_release(current, "0.5.5", "c" * 40)
