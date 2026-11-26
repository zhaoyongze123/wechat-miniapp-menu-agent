"""Velopack 适配层的本地守卫：不依赖 Windows 原生库与网络。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from menu_capture.updates.velopack_backend import (
    VelopackBackend,
    VelopackError,
    channel_name,
)


def _nupkg(*, version: str, commit: str, payload: bytes = b"print('ok')\n") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(
            "lib/net45/release-manifest.json",
            json.dumps({"version": version, "git_commit": commit,
                        "frontend_entry": "index.html", "frontend_sha256": "e" * 64}),
        )
        archive.writestr("lib/net45/app/server.py", payload)
    return buffer.getvalue()


def _setup_backend(tmp_path: Path, monkeypatch, *, version: str, commit: str,
                   payload: bytes = b"print('ok')\n"):
    packages = tmp_path / "packages"
    packages.mkdir()
    name = f"Yipu.MenuCapture-{version}-full.nupkg"
    raw = _nupkg(version=version, commit=commit, payload=payload)
    (packages / name).write_bytes(raw)
    monkeypatch.setenv("MENU_CAPTURE_VELOPACK_PACKAGES", str(packages))
    backend = VelopackBackend("owner/repo", tmp_path / "data")
    digest = hashlib.sha256(raw).hexdigest()
    backend.record_verified_package(
        version=version, commit=commit, name=name, size=len(raw),
        sha256=digest, source="signed-delta-rebuild",
    )
    return backend, packages, name, raw, digest


def _no_network(*_args, **_kwargs):
    raise AssertionError("不应访问网络或下载旧 Full")


def test_prepare_recovery_reuses_verified_rebuilt_package(tmp_path, monkeypatch):
    commit = "a" * 40
    backend, _packages, name, raw, digest = _setup_backend(
        tmp_path, monkeypatch, version="0.5.2", commit=commit,
    )
    monkeypatch.setattr(backend, "fetch_release_manifest", _no_network)
    monkeypatch.setattr(backend, "_download_verified", _no_network)

    asset = backend.prepare_recovery_package("0.5.2", commit, {"version": "0.5.3"})
    assert asset["name"] == name
    assert asset["size"] == len(raw) and asset["sha256"] == digest
    assert asset["source"]["type"] == "local-packages"
    assert Path(asset["path"]).is_file()

    # 模拟 SDK 下载成功后清理 packages 目录：安装阶段应复用已生成的恢复副本。
    (Path(_packages) / name).unlink()
    again = backend.prepare_recovery_package("0.5.2", commit, {"version": "0.5.3"})
    assert again["source"]["type"] == "recovery-cache"
    assert again["sha256"] == digest


def test_sequential_upgrades_reuse_recovery_without_downloading_full(tmp_path, monkeypatch):
    commit = "a" * 40
    packages = tmp_path / "packages"
    packages.mkdir()
    monkeypatch.setenv("MENU_CAPTURE_VELOPACK_PACKAGES", str(packages))
    backend = VelopackBackend("owner/repo", tmp_path / "data")
    monkeypatch.setattr(backend, "fetch_release_manifest", _no_network)
    monkeypatch.setattr(backend, "_download_verified", _no_network)

    installed = None
    for version in ("0.5.2", "0.5.3"):
        raw = _nupkg(version=version, commit=commit, payload=f"print('{version}')\n".encode())
        name = f"Yipu.MenuCapture-{version}-full.nupkg"
        (packages / name).write_bytes(raw)
        backend.record_verified_package(
            version=version, commit=commit, name=name, size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(), source="signed-delta-rebuild",
        )
        asset = backend.prepare_recovery_package(version, commit, {"version": "next"})
        assert asset["sha256"] == hashlib.sha256(raw).hexdigest()
        installed = asset
    assert installed is not None and installed["source"]["type"] == "local-packages"


def test_prepare_recovery_rejects_package_tampered_after_verification(tmp_path, monkeypatch):
    commit = "a" * 40
    backend, packages, name, raw, _digest = _setup_backend(
        tmp_path, monkeypatch, version="0.5.2", commit=commit,
        payload=b"print('ok')\n",
    )
    # 保持 release-manifest.json 不变，只篡改其他文件；长度相同，仅内容变化。
    (packages / name).write_bytes(
        _nupkg(version="0.5.2", commit=commit, payload=b"print('no')\n")
    )
    monkeypatch.setattr(backend, "fetch_release_manifest", _no_network)
    with pytest.raises(AssertionError, match="不应访问网络"):
        backend.prepare_recovery_package("0.5.2", commit, {"version": "0.5.3"})


def test_pending_requires_verified_record_and_actual_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", "1")
    commit = "a" * 40
    version = "0.5.2"
    backend, packages, name, raw, digest = _setup_backend(
        tmp_path, monkeypatch, version=version, commit=commit,
    )
    target = {
        "version": version, "git_commit": commit, "pack_id": "Yipu.MenuCapture",
        "full": {"name": name}, "trust": {"mode": "unsigned"},
    }
    identity = {"version": version, "pack_id": "Yipu.MenuCapture", "type": "Full",
                "file_name": name, "sha256": "", "size": len(raw)}
    monkeypatch.setattr(backend, "pending_restart", lambda channel=None: dict(identity))

    assert backend.verify_pending(target, "stable")["sha256"] == digest

    # 模拟记录丢失：本地包存在且清单未变，也必须拒绝。
    backend._records_path().unlink()
    with pytest.raises(VelopackError, match="可信下载记录"):
        backend.verify_pending(target, "stable")

    backend.record_verified_package(version=version, commit=commit, name=name,
                                    size=len(raw), sha256=digest,
                                    source="signed-delta-rebuild")
    (packages / name).write_bytes(
        _nupkg(version=version, commit=commit, payload=b"print('no')\n")
    )
    with pytest.raises(VelopackError, match="可信下载记录"):
        backend.verify_pending(target, "stable")


def test_discard_untrusted_cached_package(tmp_path, monkeypatch):
    commit = "a" * 40
    version = "0.5.2"
    packages = tmp_path / "packages"
    packages.mkdir()
    monkeypatch.setenv("MENU_CAPTURE_VELOPACK_PACKAGES", str(packages))
    backend = VelopackBackend("owner/repo", tmp_path / "data")
    name = f"Yipu.MenuCapture-{version}-full.nupkg"
    raw = _nupkg(version=version, commit=commit)
    target = {"version": version, "git_commit": commit,
              "full": {"name": name}}

    (packages / name).write_bytes(raw)
    backend.discard_untrusted_cached_package(target)
    assert not (packages / name).exists()  # 无记录：禁止复用

    (packages / name).write_bytes(raw)
    backend.record_verified_package(version=version, commit=commit, name=name,
                                    size=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                                    source="signed-full")
    backend.discard_untrusted_cached_package(target)
    assert (packages / name).exists()  # 记录与摘要一致：保留

    (packages / name).write_bytes(
        _nupkg(version=version, commit=commit, payload=b"print('no')\n")
    )
    backend.discard_untrusted_cached_package(target)
    assert not (packages / name).exists()  # 摘要不符：删除


def test_download_record_distinguishes_sdk_full_fallback(tmp_path, monkeypatch):
    from menu_capture.updates.velopack_worker import _record_downloaded_package

    commit = "a" * 40
    version = "0.5.2"
    packages = tmp_path / "packages"
    packages.mkdir()
    monkeypatch.setenv("MENU_CAPTURE_VELOPACK_PACKAGES", str(packages))
    backend = VelopackBackend("owner/repo", tmp_path / "data")
    name = f"Yipu.MenuCapture-{version}-full.nupkg"
    full = _nupkg(version=version, commit=commit, payload=b"full")
    target = {"version": version, "git_commit": commit,
              "full": {"name": name, "size": len(full),
                       "sha256": hashlib.sha256(full).hexdigest()},
              "trust": {"mode": "ed25519"}}

    # SDK 内部 delta 失败回退 full：产物等于签名 full，必须标记 signed-full。
    (packages / name).write_bytes(full)
    _record_downloaded_package(backend, target, delta_used=True)
    assert backend.verified_package(version, commit)["source"] == "signed-full"

    # 真正的差分重建产物：标记 signed-delta-rebuild。
    (packages / name).write_bytes(_nupkg(version=version, commit=commit, payload=b"rebuilt"))
    _record_downloaded_package(backend, target, delta_used=True)
    assert backend.verified_package(version, commit)["source"] == "signed-delta-rebuild"

    # 非 delta 路径却与签名 full 不一致：无既有可信记录时必须拒绝。
    backend._records_path().unlink()
    with pytest.raises(ValueError):
        _record_downloaded_package(backend, target, delta_used=False)


def test_records_reject_unknown_schema_source_and_bad_names(tmp_path, monkeypatch):
    packages = tmp_path / "packages"
    packages.mkdir()
    monkeypatch.setenv("MENU_CAPTURE_VELOPACK_PACKAGES", str(packages))
    backend = VelopackBackend("owner/repo", tmp_path / "data")
    version, commit = "0.5.2", "a" * 40
    name = f"Yipu.MenuCapture-{version}-full.nupkg"
    raw = _nupkg(version=version, commit=commit)
    (packages / name).write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    backend.record_verified_package(version=version, commit=commit, name=name,
                                    size=len(raw), sha256=digest, source="signed-full")
    path = backend._records_path()
    assert backend.verified_package(version, commit) is not None

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert backend.verified_package(version, commit) is None

    payload["schema_version"] = 1
    payload["packages"][version]["source"] = "official-release"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert backend.verified_package(version, commit) is None

    payload["packages"][version]["source"] = "signed-full"
    payload["packages"][version]["name"] = "../evil.nupkg"
    payload["packages"]["0.5.1"] = "broken"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert backend.verified_package(version, commit) is None
    # 损坏条目不得导致记录写入/排序异常。
    backend.record_verified_package(version="0.5.3", commit="c" * 40, name=name,
                                    size=len(raw), sha256=digest, source="signed-full")
    assert backend.verified_package("0.5.3", "c" * 40) is not None


def test_pending_and_discard_handle_links_safely(tmp_path, monkeypatch):
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", "1")
    version, commit = "0.5.2", "a" * 40
    packages = tmp_path / "packages"
    packages.mkdir()
    monkeypatch.setenv("MENU_CAPTURE_VELOPACK_PACKAGES", str(packages))
    backend = VelopackBackend("owner/repo", tmp_path / "data")
    name = f"Yipu.MenuCapture-{version}-full.nupkg"
    outside = tmp_path / "outside.nupkg"
    outside.write_bytes(_nupkg(version=version, commit=commit))
    (packages / name).symlink_to(outside)
    target = {"version": version, "git_commit": commit, "pack_id": "Yipu.MenuCapture",
              "full": {"name": name}, "trust": {"mode": "unsigned"}}
    monkeypatch.setattr(backend, "pending_restart", lambda channel=None: {
        "version": version, "pack_id": "Yipu.MenuCapture", "type": "Full",
        "file_name": name, "sha256": "", "size": outside.stat().st_size,
    })
    with pytest.raises(VelopackError):
        backend.verify_pending(target, "stable")
    backend.discard_untrusted_cached_package(target)
    assert not (packages / name).exists()
    assert outside.is_file()


def _release_manifest(*, repo: str, version: str, commit: str, full_bytes: bytes,
                      delta_bytes: bytes = b"delta") -> dict:
    tag = f"v{version}"

    def asset(name: str, body: bytes) -> dict:
        return {"name": name, "size": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "url": f"https://github.com/{repo}/releases/download/{tag}/{name}"}

    full_name = f"Yipu.MenuCapture-{version}-full.nupkg"
    return {
        "version": version, "channel": "stable", "git_commit": commit,
        "min_supported_version": "0.3.0", "rollback": "supported",
        "assets": [{"kind": "hub_image", "name": f"hub-{version}.tar.gz", "size": 1,
                    "sha256": "f" * 64,
                    "url": f"https://github.com/{repo}/releases/download/{tag}/hub-{version}.tar.gz",
                    "platform": "linux", "arch": "amd64"}],
        "velopack": {
            "schema_version": 1, "pack_id": "Yipu.MenuCapture", "platform": "windows",
            "arch": "x64", "channel": "win-x64-stable", "version": version,
            "git_commit": commit, "tag": tag,
            "setup": asset(f"MenuCapture-{version}-Setup.exe", b"setup"),
            "full": {"name": full_name, "size": len(full_bytes),
                     "sha256": hashlib.sha256(full_bytes).hexdigest(),
                     "url": f"https://github.com/{repo}/releases/download/{tag}/{full_name}"},
            "deltas": [asset(f"Yipu.MenuCapture-0.5.1-{version}-delta.nupkg", delta_bytes)],
            "index": asset("releases.win-x64-stable.json", b"{}"),
            "trust": {"mode": "unsigned"},
        },
    }


def test_prepare_recovery_download_cleans_temp_and_labels_source(tmp_path, monkeypatch):
    repo, version, commit = "owner/repo", "0.5.1", "a" * 40
    packages = tmp_path / "packages"
    packages.mkdir()
    monkeypatch.setenv("MENU_CAPTURE_VELOPACK_PACKAGES", str(packages))
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", "1")
    backend = VelopackBackend(repo, tmp_path / "data")
    body = _nupkg(version=version, commit=commit)
    manifest = _release_manifest(repo=repo, version=version, commit=commit, full_bytes=body)
    monkeypatch.setattr(backend, "fetch_release_manifest", lambda *_a, **_k: manifest)

    def fake_download(_asset, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        return destination

    monkeypatch.setattr(backend, "_download_verified", fake_download)
    asset = backend.prepare_recovery_package(version, commit, {"version": "0.5.2"})
    temp = backend._recovery_root() / ".download" / f"Yipu.MenuCapture-{version}-full.nupkg"
    assert not temp.exists()
    assert Path(asset["path"]).is_file()
    assert backend.verified_package(version, commit)["source"] == "unsigned-full"


def test_worker_retry_reuses_verified_rebuilt_target(tmp_path, monkeypatch):
    from menu_capture.updates import velopack_worker

    repo = "owner/repo"
    current_version, current_commit = "0.5.1", "a" * 40
    target_version, target_commit = "0.5.2", "b" * 40
    packages = tmp_path / "packages"
    packages.mkdir()
    data = tmp_path / "data"
    current = tmp_path / "current"
    current.mkdir()
    (current / "release-manifest.json").write_text(
        json.dumps({"version": current_version, "git_commit": current_commit,
                    "frontend_entry": "index.html", "frontend_sha256": "e" * 64}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MENU_CAPTURE_VELOPACK_PACKAGES", str(packages))
    monkeypatch.setenv("MENU_CAPTURE_VELOPACK_CURRENT", str(current))
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", "1")

    base_name = f"Yipu.MenuCapture-{current_version}-full.nupkg"
    base_bytes = _nupkg(version=current_version, commit=current_commit)
    target_name = f"Yipu.MenuCapture-{target_version}-full.nupkg"
    rebuilt = _nupkg(version=target_version, commit=target_commit, payload=b"rebuilt")
    # 模拟上一轮：差分重建成功、SDK 已删除旧基线、安装尚未执行。
    (packages / target_name).write_bytes(rebuilt)

    backend = VelopackBackend(repo, data)
    backend.record_verified_package(version=target_version, commit=target_commit,
                                    name=target_name, size=len(rebuilt),
                                    sha256=hashlib.sha256(rebuilt).hexdigest(),
                                    source="unsigned-delta-rebuild")
    recovery_dir = data / "recovery" / f"{current_version}-{hashlib.sha256(base_bytes).hexdigest()[:12]}"
    recovery_dir.mkdir(parents=True)
    (recovery_dir / base_name).write_bytes(base_bytes)
    backend.record_verified_package(version=current_version, commit=current_commit,
                                    name=base_name, size=len(base_bytes),
                                    sha256=hashlib.sha256(base_bytes).hexdigest(),
                                    source="unsigned-full")

    full_bytes = _nupkg(version=target_version, commit=target_commit, payload=b"official-full")
    manifest = _release_manifest(repo=repo, version=target_version, commit=target_commit,
                                 full_bytes=full_bytes)
    raw = manifest["velopack"]
    full_asset = SimpleNamespace(Version=target_version, PackageId="Yipu.MenuCapture",
                                 Type="Full", FileName=target_name,
                                 SHA256=raw["full"]["sha256"], Size=raw["full"]["size"])
    base_asset = SimpleNamespace(Version=current_version, PackageId="Yipu.MenuCapture",
                                 Type="Full", FileName=base_name,
                                 SHA256="", Size=len(base_bytes))
    delta_raw = raw["deltas"][0]
    delta_asset = SimpleNamespace(Version=target_version, PackageId="Yipu.MenuCapture",
                                  Type="Delta", FileName=delta_raw["name"],
                                  SHA256=delta_raw["sha256"], Size=delta_raw["size"])

    class FakeUpdateInfo:
        def __init__(self, target, deltas, downgrade):
            self.TargetFullRelease = target
            self.DeltasToTarget = deltas
            self.IsDowngrade = downgrade
            self.BaseRelease = None

    monkeypatch.setitem(sys.modules, "velopack", SimpleNamespace(UpdateInfo=FakeUpdateInfo))

    class FakeManager:
        def __init__(self):
            self.downloads = []

        def get_current_version(self):
            return current_version

        def check_for_updates(self):
            return SimpleNamespace(TargetFullRelease=full_asset, BaseRelease=base_asset,
                                   DeltasToTarget=[delta_asset], IsDowngrade=False)

        def download_updates(self, info, progress=None):
            self.downloads.append(info)
            if (packages / target_name).exists():
                return
            (packages / target_name).write_bytes(rebuilt)
            (packages / base_name).unlink(missing_ok=True)
            if progress:
                progress(100)

    manager = FakeManager()
    monkeypatch.setattr(VelopackBackend, "manager",
                        lambda self, channel, version: manager)
    progress = data / "progress.json"
    assert velopack_worker.main([
        "--repo", repo, "--channel", "stable", "--version", target_version,
        "--progress", str(progress), "--target-json", json.dumps(raw),
    ]) == 0
    assert json.loads(progress.read_text(encoding="utf-8"))["stage"] == "completed"
    assert manager.downloads and list(manager.downloads[0].DeltasToTarget) == []
    assert (packages / target_name).read_bytes() == rebuilt
    assert backend.verified_package(target_version, target_commit)["source"] == "unsigned-delta-rebuild"


def test_trusted_base_requires_version_name_and_digest(tmp_path, monkeypatch):
    from menu_capture.updates.velopack_worker import _trusted_base

    commit = "a" * 40
    backend, packages, name, raw, digest = _setup_backend(
        tmp_path, monkeypatch, version="0.5.2", commit=commit,
    )
    recovery = {"commit": commit, "name": name, "size": len(raw), "sha256": digest}
    ok = SimpleNamespace(BaseRelease=SimpleNamespace(Version="0.5.2", FileName=name),
                         DeltasToTarget=[object()])
    assert _trusted_base(ok, backend, recovery, "0.5.2")
    assert not _trusted_base(
        SimpleNamespace(BaseRelease=SimpleNamespace(Version="0.5.1", FileName=name),
                        DeltasToTarget=[object()]),
        backend, recovery, "0.5.2",
    )
    assert not _trusted_base(
        SimpleNamespace(BaseRelease=SimpleNamespace(Version="0.5.2", FileName="other.nupkg"),
                        DeltasToTarget=[object()]),
        backend, recovery, "0.5.2",
    )
    (packages / name).write_bytes(
        _nupkg(version="0.5.2", commit=commit, payload=b"print('no')\n")
    )
    assert not _trusted_base(ok, backend, recovery, "0.5.2")


def test_channel_name_encodes_platform_and_release_channel():
    assert channel_name("stable") == "win-x64-stable"
    assert channel_name("preview") == "win-x64-preview"
    with pytest.raises(VelopackError):
        channel_name("nightly")


def test_backend_is_unavailable_outside_windows(tmp_path: Path):
    backend = VelopackBackend("owner/repo", tmp_path)
    assert backend.available() is False
    assert backend.pending_restart() is None


def test_apply_requires_available_install_layout(tmp_path: Path):
    backend = VelopackBackend("owner/repo", tmp_path)
    with pytest.raises(VelopackError, match="Velopack 安装环境"):
        backend.apply_pending({}, channel="stable")


def test_stable_channel_uses_versioned_http_source(tmp_path, monkeypatch):
    created: dict[str, object] = {}

    class FakeHttpSource:
        def __init__(self, url):
            created["kind"] = "http"
            created["url"] = url

    class FakeGithubSource:
        def __init__(self, url, token, prerelease):
            created["kind"] = "github"
            created["url"] = url
            created["prerelease"] = prerelease

    fake = SimpleNamespace(
        HttpSource=FakeHttpSource, GithubSource=FakeGithubSource,
        UpdateOptions=lambda *args: None, UpdateManager=lambda *args: "manager",
    )
    monkeypatch.setitem(sys.modules, "velopack", fake)
    backend = VelopackBackend("owner/repo", tmp_path)
    monkeypatch.setattr(backend, "locator", lambda: None)

    assert backend.manager("stable", "0.5.1") == "manager"
    assert created == {
        "kind": "http",
        "url": "https://github.com/owner/repo/releases/download/v0.5.1/",
    }

    backend.manager("preview")
    assert created["kind"] == "github"
    assert created["prerelease"] is True

    with pytest.raises(VelopackError):
        backend.manager("stable", "../0.5.1")


def test_download_reports_failure_without_python_module(tmp_path: Path, monkeypatch):
    (tmp_path / "app").mkdir()
    backend = VelopackBackend(
        "owner/repo", tmp_path, python="/nonexistent/python", app_dir=tmp_path,
    )
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", "1")
    result = backend.download("0.4.0", target={"version": "0.4.0", "trust": {"mode": "unsigned"}}, timeout=10)
    assert result["status"] == "failed"
    assert "无法启动下载进程" in result["message"]
    assert not list(tmp_path.glob("update-download-progress-*.json"))


@pytest.mark.parametrize("value,expected", [
    (0, 0), (1, 1), (50, 50), (100, 100), (110, 100), (-1, 0),
    (float("nan"), 0), (float("inf"), 0), (None, 0),
])
def test_sdk_progress_is_percent_not_fraction(value, expected):
    from menu_capture.updates.velopack_worker import _percent

    assert _percent(value) == expected


def test_cancel_waits_after_kill():
    calls = []

    def wait(timeout):
        calls.append("wait")
        if calls.count("wait") == 1:
            raise subprocess.TimeoutExpired("python", timeout)
        return -9

    process = SimpleNamespace(
        poll=lambda: -9 if calls.count("wait") == 2 else None,
        terminate=lambda: calls.append("terminate"),
        kill=lambda: calls.append("kill"),
        wait=wait,
    )
    assert VelopackBackend._terminate(process)
    assert calls == ["terminate", "wait", "kill", "wait"]


def test_cancel_fails_if_process_does_not_exit():
    def wait(timeout):
        raise subprocess.TimeoutExpired("python", timeout)

    process = SimpleNamespace(poll=lambda: None, terminate=lambda: None, kill=lambda: None, wait=wait)
    assert not VelopackBackend._terminate(process)


def test_cancel_real_process():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert VelopackBackend._terminate(process)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_download_refuses_retry_while_previous_process_alive(tmp_path):
    backend = VelopackBackend("owner/repo", tmp_path)
    backend._download_process = SimpleNamespace(poll=lambda: None)
    result = backend.download("0.4.0")
    assert result["status"] == "failed"
    assert "尚未退出" in result["message"]


def test_download_requires_bound_target(tmp_path):
    backend = VelopackBackend("owner/repo", tmp_path)
    result = backend.download("0.4.0")
    assert result["status"] == "failed"
    assert "目标" in result["message"]
