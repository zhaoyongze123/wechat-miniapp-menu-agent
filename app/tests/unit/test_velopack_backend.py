"""Velopack 适配层的本地守卫：不依赖 Windows 原生库与网络。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from menu_capture.updates.velopack_backend import (
    VelopackBackend,
    VelopackError,
    channel_name,
)


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
