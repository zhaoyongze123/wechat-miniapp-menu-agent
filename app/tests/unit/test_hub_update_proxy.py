"""Hub 更新代理的 OCI 能力协商：只有执行器明确支持时才发送 image/digest。"""

from __future__ import annotations

import time
from typing import Any

from menu_capture.shared.hub_update import HubInstallProxy

REGISTRY = "ghcr.io/zhaoyongze123/wechat-miniapp-menu-agent"
DIGEST = "sha256:" + "a" * 64
ASSET = {
    "kind": "hub_image",
    "name": "hub-image-0.3.2.tar.gz",
    "size": 1024,
    "sha256": "b" * 64,
    "url": "https://github.com/zhaoyongze123/wechat-miniapp-menu-agent/releases/download/v0.3.2/hub-image-0.3.2.tar.gz",
    "platform": "linux",
    "arch": "amd64",
    "registry": REGISTRY,
    "digest": DIGEST,
}


class FakeExecutor:
    def __init__(self, *, capabilities: dict | None = None) -> None:
        self.capabilities = capabilities
        self.calls: list[tuple[str, str, dict | None]] = []
        self.applied: dict | None = None

    def __call__(self, method: str, path: str, payload: dict | None, timeout: float) -> dict:
        self.calls.append((method, path, payload))
        if method == "GET" and path == "/status":
            data: dict[str, Any] = {
                "active": False, "status": "idle", "target_version": "",
                "previous_version": "", "message": "", "error": "",
                "started_at": "", "finished_at": "",
            }
            if self.capabilities is not None:
                data["capabilities"] = self.capabilities
            return data
        if method == "POST" and path == "/apply":
            self.applied = payload
            return {"status": "switching", "active": True}
        raise AssertionError(f"unexpected request: {method} {path}")


def make_proxy(executor: FakeExecutor, *, asset: dict | None = None) -> HubInstallProxy:
    latest = {"version": "0.3.2", "assets": [dict(asset or ASSET)]}
    return HubInstallProxy(
        base_url="http://127.0.0.1:8767",
        token="t" * 32,
        latest_provider=lambda: latest,
        request=executor,
        timeout=0.1,
    )


def submit(proxy: HubInstallProxy, executor: FakeExecutor) -> dict:
    """提交后等待后台线程完成请求，返回真正发给执行器的请求体。"""
    proxy.start("0.3.2", "0.3.2")
    deadline = time.monotonic() + 2
    while executor.applied is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert executor.applied is not None
    return executor.applied


def test_old_executor_without_capabilities_gets_archive_only() -> None:
    executor = FakeExecutor(capabilities=None)
    proxy = make_proxy(executor)
    proxy.status(force=True)

    payload = submit(proxy, executor)

    assert "image" not in payload and "digest" not in payload
    assert payload["url"].startswith("https://github.com/")


def test_oci_capable_executor_receives_pinned_registry_and_digest() -> None:
    executor = FakeExecutor(capabilities={"oci": True})
    proxy = make_proxy(executor)
    proxy.status(force=True)

    payload = submit(proxy, executor)

    assert payload["image"] == REGISTRY
    assert payload["digest"] == DIGEST
    # 归档字段必须保留，执行器拉取失败时仍可回退。
    assert payload["url"] and payload["sha256"] and payload["size"] > 0


def test_invalid_registry_extension_is_ignored_without_blocking_update() -> None:
    executor = FakeExecutor(capabilities={"oci": True})
    asset = {**ASSET, "registry": "docker.io/owner/repo"}
    proxy = make_proxy(executor, asset=asset)
    proxy.status(force=True)

    payload = submit(proxy, executor)

    assert "image" not in payload and "digest" not in payload
    assert payload["version"] == "0.3.2"


def test_capability_snapshot_survives_submission() -> None:
    executor = FakeExecutor(capabilities={"oci": True})
    proxy = make_proxy(executor)
    proxy.status(force=True)

    submit(proxy, executor)

    assert proxy.oci_supported is True
