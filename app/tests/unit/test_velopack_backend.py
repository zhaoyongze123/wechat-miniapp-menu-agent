"""Velopack 适配层的本地守卫：不依赖 Windows 原生库与网络。"""
from __future__ import annotations

import json
from pathlib import Path

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


def test_apply_request_and_state_round_trip(tmp_path: Path):
    backend = VelopackBackend("owner/repo", tmp_path)
    backend.request_apply("0.4.0")
    request = backend.read_apply_request()
    assert request["version"] == "0.4.0"
    assert request["channel"] == "stable"

    backend.write_apply_state("failed", "0.4.0", message="未下载", error="missing")
    state = backend.read_apply_state()
    assert state == {
        "status": "failed",
        "version": "0.4.0",
        "message": "未下载",
        "error": "missing",
        "updated_at": state["updated_at"],
    }

    backend.clear_apply_request()
    assert backend.read_apply_request() is None


def test_apply_requires_available_install_layout(tmp_path: Path):
    backend = VelopackBackend("owner/repo", tmp_path)
    with pytest.raises(VelopackError, match="Velopack 安装环境"):
        backend.apply_pending("0.4.0")


def test_download_reports_failure_without_python_module(tmp_path: Path):
    (tmp_path / "app").mkdir()
    backend = VelopackBackend(
        "owner/repo", tmp_path, python="/nonexistent/python", app_dir=tmp_path,
    )
    result = backend.download("0.4.0", timeout=10)
    assert result["status"] == "failed"
    progress = json.loads((tmp_path / "update-download-progress.json").read_text(encoding="utf-8"))
    assert progress["stage"] == "failed"
    assert "无法启动下载进程" in progress["error"]
