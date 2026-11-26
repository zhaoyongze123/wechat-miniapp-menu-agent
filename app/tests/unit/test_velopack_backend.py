"""Velopack 适配层的本地守卫：不依赖 Windows 原生库与网络。"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
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
                       "sha256": hashlib.sha256(full).hexdigest()}}

    # SDK 内部 delta 失败回退 full：产物等于签名 full，必须标记 signed-full。
    (packages / name).write_bytes(full)
    _record_downloaded_package(backend, target, delta_used=True)
    assert backend.verified_package(version, commit)["source"] == "signed-full"

    # 真正的差分重建产物：标记 signed-delta-rebuild。
    (packages / name).write_bytes(_nupkg(version=version, commit=commit, payload=b"rebuilt"))
    _record_downloaded_package(backend, target, delta_used=True)
    assert backend.verified_package(version, commit)["source"] == "signed-delta-rebuild"

    # 非 delta 路径却与签名 full 不一致：拒绝，且不得把未知来源写成可信。
    with pytest.raises(ValueError):
        _record_downloaded_package(backend, target, delta_used=False)
    assert backend.verified_package(version, commit)["source"] == "signed-delta-rebuild"


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
