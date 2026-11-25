"""更新发现、清单校验、缓存与本地版本保护规则。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from menu_capture.updates import updates as updates_module
from menu_capture.updates.updates import CACHE_TTL_SECONDS, UpdateService, Version, is_newer


@pytest.fixture(autouse=True)
def _clean_env():
    yield
    os.environ.pop("MENU_CAPTURE_VERSION", None)

REPO = "zhaoyongze123/wechat-miniapp-menu-agent"
BASE = f"https://github.com/{REPO}/releases/download"


def release(tag: str, *, manifest: dict | None, prerelease: bool = False, draft: bool = False,
            missing_manifest: bool = False) -> dict:
    assets = []
    if not missing_manifest:
        manifest_body = json.dumps(manifest or {}, ensure_ascii=False).encode()
        assets.append({
            "name": "update-manifest.json",
            "size": len(manifest_body),
            "browser_download_url": f"{BASE}/{tag}/update-manifest.json",
            "digest": "",
        })
    assets.append({
        "name": f"MenuCapture-{tag}.exe",
        "size": 1024,
        "browser_download_url": f"{BASE}/{tag}/MenuCapture-{tag}.exe",
        "digest": "sha256:" + "a" * 64,
    })
    return {
        "tag_name": tag,
        "name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
        "published_at": "2026-11-24T00:00:00Z",
        "body": f"{tag} 更新说明",
        "assets": assets,
    }


def manifest(version: str, channel: str = "stable") -> dict:
    return {
        "version": version,
        "channel": channel,
        "git_commit": "b" * 40,
        "built_at": "2026-11-24T00:00:00Z",
        "min_supported_version": "0.1.0",
        "rollback": "supported",
        "assets": [{
            "kind": "windows_installer",
            "name": f"MenuCapture-v{version}.exe",
            "size": 1024,
            "sha256": "a" * 64,
            "url": f"{BASE}/v{version}/MenuCapture-v{version}.exe",
            "platform": "windows",
            "arch": "x64",
        }],
    }


class Fetcher:
    def __init__(self, releases, manifests):
        self.releases = releases
        self.manifests = manifests
        self.calls: list[str] = []
        self.fail = False

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        if self.fail:
            raise OSError("network down")
        if "api.github.com" in url:
            return 200, {"ETag": '"v1"'}, json.dumps(self.releases).encode()
        if url.endswith("update-manifest.json"):
            tag = url.split("/releases/download/")[1].split("/")[0]
            body = json.dumps(self.manifests[tag], ensure_ascii=False).encode()
            return 200, {}, body
        raise AssertionError(f"unexpected url {url}")


class BlockingFetcher(Fetcher):
    """模拟慢网络：在 GitHub 列表请求处阻塞，用于验证状态查询不被锁阻塞。"""

    def __init__(self, releases, manifests):
        super().__init__(releases, manifests)
        self.entered = threading.Event()
        self.release_event = threading.Event()

    def __call__(self, url, headers, timeout):
        if "api.github.com" in url:
            self.entered.set()
            self.release_event.wait(5)
        return super().__call__(url, headers, timeout)


OCI_REGISTRY = "ghcr.io/zhaoyongze123/wechat-miniapp-menu-agent"
OCI_DIGEST = "sha256:" + "d" * 64


def hub_manifest(version: str = "0.3.0", *, registry: str | None = OCI_REGISTRY,
                 digest: str | None = OCI_DIGEST) -> dict:
    asset = {
        "kind": "hub_image",
        "name": f"hub-image-{version}.tar.gz",
        "size": 2048,
        "sha256": "c" * 64,
        "url": f"{BASE}/v{version}/hub-image-{version}.tar.gz",
        "platform": "linux",
        "arch": "amd64",
    }
    if registry is not None or digest is not None:
        asset["registry"] = registry
        asset["digest"] = digest
    return {
        "version": version,
        "channel": "stable",
        "git_commit": "b" * 40,
        "built_at": "2026-11-24T00:00:00Z",
        "min_supported_version": "0.1.0",
        "rollback": "supported",
        "assets": [asset],
    }


def hub_release(tag: str, body: dict) -> dict:
    manifest_body = json.dumps(body, ensure_ascii=False).encode()
    return {
        "tag_name": tag,
        "name": tag,
        "draft": False,
        "prerelease": False,
        "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
        "published_at": "2026-11-24T00:00:00Z",
        "body": "",
        "assets": [
            {
                "name": "update-manifest.json",
                "size": len(manifest_body),
                "browser_download_url": f"{BASE}/{tag}/update-manifest.json",
                "digest": "",
            },
            {
                "name": f"hub-image-{body['version']}.tar.gz",
                "size": 2048,
                "browser_download_url": f"{BASE}/{tag}/hub-image-{body['version']}.tar.gz",
                "digest": "sha256:" + "c" * 64,
            },
        ],
    }


def make_hub_service(tmp_path: Path, body: dict, *, current: str = "0.2.0") -> UpdateService:
    tag = "v" + body["version"]
    service = UpdateService(
        tmp_path, role="hub", fetcher=Fetcher([hub_release(tag, body)], {tag: body}),
        platform="linux", machine="x86_64",
    )
    service.save_preferences({"channel": "stable"})
    os.environ["MENU_CAPTURE_VERSION"] = current
    return service


def make_service(tmp_path: Path, releases, manifests, *, current="0.2.0", channel="stable", fetcher=None,
                 machine="AMD64", opener=None, install_root=None):
    service = UpdateService(
        tmp_path, role="worker", fetcher=fetcher or Fetcher(releases, manifests),
        opener=opener, platform="windows", machine=machine, install_root=install_root,
    )
    service.save_preferences({"channel": channel})
    service._cache = {}
    if current:
        import os
        os.environ["MENU_CAPTURE_VERSION"] = current
    return service


def test_semver_orders_prerelease_before_stable():
    assert not (Version.parse("0.2.0-rc.10") < Version.parse("0.2.0-rc.2"))
    assert Version.parse("0.2.0-rc.2") < Version.parse("0.2.0-rc.10")
    assert Version.parse("0.2.0-rc.3") < Version.parse("0.2.0")
    assert is_newer("0.3.0", "0.2.0")
    assert not is_newer("0.2.0-rc.3", "0.2.0")


def test_stable_channel_ignores_rc_and_draft(tmp_path):
    stable = release("v0.3.0", manifest=manifest("0.3.0"))
    rc = release("v0.4.0-rc.1", manifest=manifest("0.4.0-rc.1", "preview"), prerelease=True)
    draft = release("v0.5.0", manifest=manifest("0.5.0"), draft=True)
    fetcher = Fetcher([draft, rc, stable], {"v0.3.0": manifest("0.3.0")})
    service = make_service(tmp_path, [draft, rc, stable], {"v0.3.0": manifest("0.3.0")}, fetcher=fetcher)

    status = service.check(force=True)

    assert status["latest"]["version"] == "0.3.0"
    assert status["update_available"] is True
    assert all("v0.4.0" not in url and "v0.5.0" not in url for url in fetcher.calls)


def test_preview_channel_can_use_rc(tmp_path):
    stable = release("v0.3.0", manifest=manifest("0.3.0"))
    rc = release("v0.4.0-rc.1", manifest=manifest("0.4.0-rc.1", "preview"), prerelease=True)
    service = make_service(
        tmp_path,
        [stable, rc],
        {"v0.3.0": manifest("0.3.0"), "v0.4.0-rc.1": manifest("0.4.0-rc.1", "preview")},
        channel="preview",
    )

    status = service.check(force=True)

    assert status["latest"]["version"] == "0.4.0-rc.1"
    assert status["update_available"] is True


def test_preview_channel_accepts_newer_stable(tmp_path):
    stable = release("v0.3.0", manifest=manifest("0.3.0"))
    rc = release("v0.2.5-rc.1", manifest=manifest("0.2.5-rc.1", "preview"), prerelease=True)
    service = make_service(
        tmp_path,
        [stable, rc],
        {"v0.3.0": manifest("0.3.0"), "v0.2.5-rc.1": manifest("0.2.5-rc.1", "preview")},
        channel="preview",
    )

    status = service.check(force=True)

    assert status["latest"]["version"] == "0.3.0"
    assert status["latest"]["channel"] == "stable"
    assert status["update_available"] is True


def test_missing_manifest_is_not_offered(tmp_path):
    rel = release("v0.3.0", manifest=None, missing_manifest=True)
    service = make_service(tmp_path, [rel], {}, fetcher=Fetcher([rel], {}))

    status = service.check(force=True)

    assert status["latest"]["eligible"] is False
    assert "update-manifest.json" in status["latest"]["reason"]
    assert status["update_available"] is False


def test_manifest_is_required_to_match_tag_and_assets(tmp_path):
    bad = manifest("0.3.0")
    bad["assets"][0]["sha256"] = "not-a-sha"
    rel = release("v0.3.0", manifest=bad)
    service = make_service(tmp_path, [rel], {"v0.3.0": bad}, fetcher=Fetcher([rel], {"v0.3.0": bad}))

    status = service.check(force=True)

    assert status["latest"]["eligible"] is False
    assert "SHA-256" in status["latest"]["reason"]


def test_wrong_platform_asset_is_rejected(tmp_path):
    bad = manifest("0.3.0")
    bad["assets"][0]["platform"] = "linux"
    rel = release("v0.3.0", manifest=bad)
    service = make_service(tmp_path, [rel], {"v0.3.0": bad}, fetcher=Fetcher([rel], {"v0.3.0": bad}))

    status = service.check(force=True)

    assert status["latest"]["eligible"] is False
    assert "平台" in status["latest"]["reason"]


def test_older_release_never_offers_downgrade(tmp_path):
    rel = release("v0.1.0", manifest=manifest("0.1.0"))
    service = make_service(tmp_path, [rel], {"v0.1.0": manifest("0.1.0")})

    status = service.check(force=True)

    assert status["latest"]["version"] == "0.1.0"
    assert status["update_available"] is False


def test_min_supported_version_blocks_direct_update(tmp_path):
    payload = manifest("0.3.0")
    payload["min_supported_version"] = "0.2.5"
    rel = release("v0.3.0", manifest=payload)
    service = make_service(tmp_path, [rel], {"v0.3.0": payload}, current="0.2.0")

    status = service.check(force=True)

    assert status["latest"]["eligible"] is True
    assert status["update_available"] is False


def test_cache_avoids_repeat_and_failure_keeps_last_good(tmp_path):
    rel = release("v0.3.0", manifest=manifest("0.3.0"))
    fetcher = Fetcher([rel], {"v0.3.0": manifest("0.3.0")})
    service = make_service(tmp_path, [rel], {"v0.3.0": manifest("0.3.0")}, fetcher=fetcher)
    service.check(force=True)
    calls = len(fetcher.calls)

    service.status()
    assert len(fetcher.calls) == calls

    fetcher.fail = True
    status = service.check(force=True)
    assert status["latest"]["version"] == "0.3.0"
    assert "network down" in status["error"]


def test_channel_switch_invalidates_cached_release(tmp_path):
    rel = release("v0.3.0", manifest=manifest("0.3.0"))
    service = make_service(tmp_path, [rel], {"v0.3.0": manifest("0.3.0")})
    service.check(force=True)

    service.save_preferences({"channel": "preview"})

    assert service._cache.get("latest") is None
    assert service._cache.get("checked_at") == 0


def _join_release_size_scan(service: UpdateService, timeout: float = 10.0) -> None:
    """等待后台体积统计线程结束，把异步补齐变成测试可断言的确定状态。"""
    thread = service._release_size_thread
    if thread is not None:
        thread.join(timeout)


def test_inventory_protects_current_and_recent_two(tmp_path, monkeypatch):
    install = tmp_path / "install"
    current_app = install / "releases" / "0.2.0" / "app"
    current_app.mkdir(parents=True)
    for version in ["0.1.9", "0.1.8", "0.1.7"]:
        root = install / "releases" / version / "app"
        root.mkdir(parents=True)
        (install / "releases" / version / "release-manifest.json").write_text(
            json.dumps({"version": version, "built_at": "2026-01-01", "git_commit": "c" * 40}), encoding="utf-8"
        )
    (install / "releases" / "0.2.0" / "release-manifest.json").write_text(
        json.dumps({"version": "0.2.0", "built_at": "2026-02-01", "git_commit": "d" * 40}), encoding="utf-8"
    )
    (install / "current-version").write_text("0.2.0", encoding="utf-8")
    # 数据目录下即使存在同名 releases 目录也不能被当作安装根。
    decoy = tmp_path / "runtime" / "releases" / "9.9.9" / "app"
    decoy.mkdir(parents=True)
    monkeypatch.setenv("MENU_CAPTURE_VERSION", "0.2.0")
    monkeypatch.setattr("menu_capture.updates.updates.sys.platform", "win32")

    # 数据目录与程序目录分离：data_root 只存缓存/审计，安装根由 app_root 推导。
    service = UpdateService(tmp_path / "runtime", role="worker", app_root=current_app, machine="AMD64")
    inventory = service.inventory()

    assert inventory["supported"] is True
    assert inventory["install_root"] == str(install)
    assert [row["version"] for row in inventory["releases"]] == ["0.2.0", "0.1.9", "0.1.8", "0.1.7"]
    protected = {row["version"] for row in inventory["releases"] if row["protected"]}
    assert protected == {"0.2.0", "0.1.9", "0.1.8"}
    assert [row["version"] for row in inventory["cleanup_candidates"]] == ["0.1.7"]
    # 清点本身只读；删除入口由 cleanup() 提供并要求显式确认。
    assert inventory["cleanup_supported"] is True
    assert service.data_dir == tmp_path / "runtime" / "data"


def test_history_records_preferences_and_check(tmp_path):
    rel = release("v0.3.0", manifest=manifest("0.3.0"))
    service = make_service(tmp_path, [rel], {"v0.3.0": manifest("0.3.0")})
    service.check(force=True)
    service.save_preferences({"auto_check": False})

    history = service.history()
    assert [row["action"] for row in history[:2]] == ["preferences", "check"]


def test_stale_is_based_on_cache_age(tmp_path):
    now = [1000.0]
    service = UpdateService(tmp_path, role="worker", fetcher=Fetcher([], {}), clock=lambda: now[0])
    service.save_preferences({"auto_check": False})

    service._cache = {"checked_at": now[0] - CACHE_TTL_SECONDS - 1, "latest": None}
    assert service.status()["stale"] is True

    service._cache = {"checked_at": now[0] - 10, "latest": None}
    assert service.status()["stale"] is False


def test_status_is_not_blocked_by_slow_network(tmp_path):
    rel = release("v0.3.0", manifest=manifest("0.3.0"))
    fetcher = BlockingFetcher([rel], {"v0.3.0": manifest("0.3.0")})
    service = make_service(tmp_path, [rel], {"v0.3.0": manifest("0.3.0")}, fetcher=fetcher)

    worker = threading.Thread(target=service.check, kwargs={"force": True})
    worker.start()
    assert fetcher.entered.wait(2)

    started = time.monotonic()
    status = service.status()
    elapsed = time.monotonic() - started
    assert elapsed < 0.5
    assert status["checking"] is True

    fetcher.release_event.set()
    worker.join(5)
    assert service.status()["checking"] is False


def test_invalid_version_is_rejected():
    with pytest.raises(ValueError):
        Version.parse("not-a-version")


def test_arch_mismatch_is_not_offered(tmp_path):
    rel = release("v0.3.0", manifest=manifest("0.3.0"))
    service = make_service(tmp_path, [rel], {"v0.3.0": manifest("0.3.0")}, machine="ARM64")

    status = service.check(force=True)

    assert status["arch"] == "arm64"
    assert status["latest"]["eligible"] is True
    assert status["update_available"] is False


def test_arch_aliases_match_x64_assets(tmp_path):
    payload = manifest("0.3.0")
    payload["assets"][0]["arch"] = "amd64"
    rel = release("v0.3.0", manifest=payload)
    service = make_service(tmp_path, [rel], {"v0.3.0": payload}, machine="x86_64")

    status = service.check(force=True)

    assert status["arch"] == "x64"
    assert status["update_available"] is True


def test_manual_check_reuses_inflight_request(tmp_path):
    rel = release("v0.3.0", manifest=manifest("0.3.0"))
    fetcher = BlockingFetcher([rel], {"v0.3.0": manifest("0.3.0")})
    service = make_service(tmp_path, [rel], {"v0.3.0": manifest("0.3.0")}, fetcher=fetcher)

    first = threading.Thread(target=service.check, kwargs={"force": True})
    first.start()
    assert fetcher.entered.wait(2)

    second_results: list[dict] = []
    second = threading.Thread(target=lambda: second_results.append(service.check(force=True)))
    second.start()
    time.sleep(0.2)
    fetcher.release_event.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    api_calls = [url for url in fetcher.calls if "api.github.com" in url]
    assert len(api_calls) == 1
    assert second_results[0]["latest"]["version"] == "0.3.0"


def test_channel_switch_discards_inflight_result(tmp_path):
    stable = release("v0.3.0", manifest=manifest("0.3.0"))
    rc = release("v0.4.0-rc.1", manifest=manifest("0.4.0-rc.1", "preview"), prerelease=True)
    fetcher = BlockingFetcher(
        [stable, rc],
        {"v0.3.0": manifest("0.3.0"), "v0.4.0-rc.1": manifest("0.4.0-rc.1", "preview")},
    )
    service = make_service(
        tmp_path, [stable, rc],
        {"v0.3.0": manifest("0.3.0"), "v0.4.0-rc.1": manifest("0.4.0-rc.1", "preview")},
        fetcher=fetcher, channel="stable",
    )

    worker = threading.Thread(target=service.check, kwargs={"force": True})
    worker.start()
    assert fetcher.entered.wait(2)

    service.save_preferences({"channel": "preview"})
    fetcher.release_event.set()
    worker.join(5)

    # 旧 stable 请求返回后不得写回新 preview 渠道的缓存，也不记录为失败。
    assert not worker.is_alive()
    assert service._cache.get("latest") is None
    assert service._cache.get("checked_at") == 0
    assert not service._cache.get("error")
    status = service.status_no_check()
    assert status["channel"] == "preview"
    assert status["checking"] is False


# ---------- 受控下载 ----------


def download_manifest(version: str, body: bytes) -> dict:
    name = f"MenuCapture-v{version}.exe"
    return {
        "version": version,
        "channel": "stable",
        "git_commit": "b" * 40,
        "built_at": "2026-11-24T00:00:00Z",
        "min_supported_version": "0.1.0",
        "rollback": "supported",
        "assets": [{
            "kind": "windows_installer",
            "name": name,
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "url": f"{BASE}/v{version}/{name}",
            "platform": "windows",
            "arch": "x64",
        }],
    }


def download_release(tag: str, payload: dict, body: bytes) -> dict:
    asset = payload["assets"][0]
    return {
        "tag_name": tag,
        "name": tag,
        "draft": False,
        "prerelease": False,
        "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
        "published_at": "2026-11-24T00:00:00Z",
        "body": f"{tag} 更新说明",
        "assets": [
            {
                "name": "update-manifest.json",
                "size": len(json.dumps(payload).encode()),
                "browser_download_url": f"{BASE}/{tag}/update-manifest.json",
                "digest": "",
            },
            {
                "name": asset["name"],
                "size": len(body),
                "browser_download_url": asset["url"],
                "digest": "sha256:" + asset["sha256"],
            },
        ],
    }


class FakeOpener:
    """返回可控响应体，并记录请求头以验证 Range 续传。"""

    def __init__(self, body: bytes, *, status: int | None = None, ignore_range: bool = False):
        self.body = body
        self.status = status
        self.ignore_range = ignore_range
        self.headers: list[dict] = []

    @contextmanager
    def __call__(self, url, headers, timeout):
        self.headers.append(dict(headers))
        start = 0
        if headers.get("Range") and not self.ignore_range:
            start = int(headers["Range"].split("=", 1)[1].strip("-"))
        if start:
            yield 206, {
                "Content-Range": f"bytes {start}-{len(self.body) - 1}/{len(self.body)}",
                "Content-Length": str(len(self.body) - start),
            }, io.BytesIO(self.body[start:])
        else:
            yield self.status or 200, {"Content-Length": str(len(self.body))}, io.BytesIO(self.body)


class SlowResponse:
    """首块正常返回，后续 read 阻塞，用于制造可取消的下载窗口。"""

    def __init__(self, body: bytes, gate: threading.Event):
        self._body = body
        self._pos = 0
        self._gate = gate
        self._first = True

    def read(self, size: int) -> bytes:
        if not self._first:
            self._gate.wait(5)
        self._first = False
        chunk = self._body[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk


class GatedOpener:
    def __init__(self, body: bytes):
        self.body = body
        self.gate = threading.Event()

    @contextmanager
    def __call__(self, url, headers, timeout):
        yield 200, {}, SlowResponse(self.body, self.gate)


class BlockingOpener:
    """在打开响应处阻塞，用于验证同一时刻只允许一个下载任务。"""

    def __init__(self, body: bytes):
        self.body = body
        self.gate = threading.Event()
        self.entered = threading.Event()

    @contextmanager
    def __call__(self, url, headers, timeout):
        self.entered.set()
        self.gate.wait(5)
        yield 200, {}, io.BytesIO(self.body)


def wait_download(service: UpdateService, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = service.download_status()
        if not state["active"]:
            return state
        time.sleep(0.01)
    raise AssertionError("下载未在超时内结束")


def prepared_service(tmp_path: Path, body: bytes, opener, **kwargs) -> UpdateService:
    payload = download_manifest("0.3.0", body)
    rel = download_release("v0.3.0", payload, body)
    service = make_service(tmp_path, [rel], {"v0.3.0": payload}, opener=opener, **kwargs)
    service.check(force=True)
    assert service.status_no_check()["update_available"] is True
    return service


ASSET_NAME = "MenuCapture-v0.3.0.exe"


def test_download_verifies_sha_and_completes(tmp_path):
    body = b"installer" * 2000
    service = prepared_service(tmp_path, body, FakeOpener(body))

    service.start_download()
    state = wait_download(service)

    assert state["status"] == "completed"
    assert state["downloaded"] == len(body)
    target = Path(state["path"])
    assert target.read_bytes() == body
    assert not target.with_name(target.name + ".part").exists()
    assert service.download_status()["resumable"] is False
    history = service.history()
    assert history[0]["action"] == "download"
    assert history[0]["result"] == "ok"


def test_download_resumes_from_partial_file(tmp_path):
    body = b"0123456789" * 500
    opener = FakeOpener(body)
    service = prepared_service(tmp_path, body, opener)
    part = service.download_dir / "0.3.0" / (ASSET_NAME + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    prefix = body[:777]
    part.write_bytes(prefix)

    service.start_download()
    state = wait_download(service)

    assert state["status"] == "completed"
    assert opener.headers[0]["Range"] == f"bytes={len(prefix)}-"
    assert Path(state["path"]).read_bytes() == body


def test_download_rejects_sha_mismatch(tmp_path):
    body = b"expected-body-1234567890"
    corrupt = b"tampered-body-1234567890"
    assert len(body) == len(corrupt)
    service = prepared_service(tmp_path, body, FakeOpener(corrupt))

    service.start_download()
    state = wait_download(service)

    assert state["status"] == "failed"
    assert "SHA-256" in state["error"]
    assert not (service.download_dir / "0.3.0" / (ASSET_NAME + ".part")).exists()
    assert service.history()[0]["result"] == "failed"


def test_download_cancel_keeps_partial_for_resume(tmp_path, monkeypatch):
    monkeypatch.setattr("menu_capture.updates.updates.DOWNLOAD_CHUNK_BYTES", 8)
    body = b"abcdefgh" * 10
    opener = GatedOpener(body)
    service = prepared_service(tmp_path, body, opener)

    service.start_download()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and service.download_status()["downloaded"] == 0:
        time.sleep(0.01)
    assert service.download_status()["downloaded"] == 8
    service.cancel_download()
    opener.gate.set()

    state = wait_download(service)
    assert state["status"] == "cancelled"
    assert state["resumable"] is True
    assert (service.download_dir / "0.3.0" / (ASSET_NAME + ".part")).read_bytes() == body[:8]


def test_download_is_single_flight(tmp_path):
    body = b"x" * 64
    opener = BlockingOpener(body)
    service = prepared_service(tmp_path, body, opener)

    service.start_download()
    assert opener.entered.wait(2)
    with pytest.raises(ValueError, match="已有下载任务"):
        service.start_download()
    opener.gate.set()

    assert wait_download(service)["status"] == "completed"


def test_download_requires_available_update(tmp_path):
    service = make_service(tmp_path, [], {}, opener=FakeOpener(b"x"))

    with pytest.raises(ValueError, match="当前没有可下载的更新"):
        service.start_download()


def test_download_prechecks_disk_space(tmp_path, monkeypatch):
    body = b"y" * 100
    opener = FakeOpener(body)
    service = prepared_service(tmp_path, body, opener)
    monkeypatch.setattr("menu_capture.updates.updates.shutil.disk_usage",
                        lambda path: SimpleNamespace(free=0))

    service.start_download()
    state = wait_download(service)

    assert state["status"] == "failed"
    assert "磁盘剩余空间不足" in state["error"]
    assert opener.headers == []


def test_download_restarts_when_server_ignores_range(tmp_path):
    body = b"0123456789" * 100
    opener = FakeOpener(body, ignore_range=True)
    service = prepared_service(tmp_path, body, opener)
    part = service.download_dir / "0.3.0" / (ASSET_NAME + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"garbage-prefix")

    service.start_download()
    state = wait_download(service)

    assert state["status"] == "completed"
    assert opener.headers[0]["Range"] == "bytes=14-"
    assert Path(state["path"]).read_bytes() == body


def test_completed_download_is_not_refetched(tmp_path):
    body = b"reuse" * 100
    opener = FakeOpener(body)
    service = prepared_service(tmp_path, body, opener)
    service.start_download()
    wait_download(service)
    calls = len(opener.headers)

    service.start_download()
    state = wait_download(service)

    assert state["status"] == "completed"
    assert len(opener.headers) == calls


def test_interrupted_download_is_reported_after_restart(tmp_path):
    body = b"z" * 50
    (tmp_path / "data").mkdir(parents=True)
    (tmp_path / "data/update-download.json").write_text(json.dumps({
        "active": True,
        "status": "downloading",
        "version": "0.3.0",
        "asset_kind": "windows_installer",
        "asset_name": ASSET_NAME,
        "downloaded": 10,
        "total": 50,
        "started_at": "",
        "finished_at": "",
        "error": "",
        "path": "",
    }), encoding="utf-8")
    part = tmp_path / "data/updates/0.3.0" / (ASSET_NAME + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(body[:10])

    service = UpdateService(tmp_path, role="worker", opener=FakeOpener(body),
                            platform="windows", machine="AMD64")

    state = service.download_status()
    assert state["status"] == "interrupted"
    assert state["active"] is False
    assert state["resumable"] is True


class WrongRangeOpener:
    """首次续传返回与断点不一致的范围，只有完整重下才返回正确内容。"""

    def __init__(self, body: bytes):
        self.body = body
        self.calls: list[dict] = []

    @contextmanager
    def __call__(self, url, headers, timeout):
        self.calls.append(dict(headers))
        if headers.get("Range"):
            yield 206, {"Content-Range": "bytes 5-9/1000", "Content-Length": "5"}, io.BytesIO(b"wrong")
        else:
            yield 200, {"Content-Length": str(len(self.body))}, io.BytesIO(self.body)


def test_resume_restarts_when_content_range_is_invalid(tmp_path):
    body = b"range-check" * 100
    opener = WrongRangeOpener(body)
    service = prepared_service(tmp_path, body, opener)
    part = service.download_dir / "0.3.0" / (ASSET_NAME + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(body[:10])

    service.start_download()
    state = wait_download(service)

    assert state["status"] == "completed"
    assert len(opener.calls) == 2
    assert "Range" not in opener.calls[1]
    assert Path(state["path"]).read_bytes() == body


def test_resume_sends_if_range_from_previous_etag(tmp_path):
    body = b"etag-body" * 200
    opener = FakeOpener(body)
    service = prepared_service(tmp_path, body, opener)
    part = service.download_dir / "0.3.0" / (ASSET_NAME + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(body[:50])
    service._download_state.update({
        "version": "0.3.0",
        "asset_name": ASSET_NAME,
        "sha256": hashlib.sha256(body).hexdigest(),
        "etag": '"v1"',
    })

    service.start_download()
    state = wait_download(service)

    assert state["status"] == "completed"
    assert opener.headers[0]["If-Range"] == '"v1"'
    assert Path(state["path"]).read_bytes() == body


def test_ignored_range_rechecks_disk_for_full_size(tmp_path, monkeypatch):
    body = b"z" * 100
    opener = FakeOpener(body, ignore_range=True)
    service = prepared_service(tmp_path, body, opener)
    part = service.download_dir / "0.3.0" / (ASSET_NAME + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(body[:10])
    calls: list[int] = []

    def fake_usage(path):
        calls.append(1)
        # 第一次续传预检通过；忽略 Range 后需要完整文件大小，第二次返回不足。
        return SimpleNamespace(free=10 ** 12) if len(calls) == 1 else SimpleNamespace(free=0)

    monkeypatch.setattr("menu_capture.updates.updates.shutil.disk_usage", fake_usage)
    service.start_download()
    state = wait_download(service)

    assert state["status"] == "failed"
    assert "磁盘剩余空间不足" in state["error"]


def test_redirect_handler_rejects_untrusted_hosts():
    from menu_capture.updates.updates import _TrustedRedirectHandler

    handler = _TrustedRedirectHandler()
    request = Request("https://github.com/zhaoyongze123/example/releases/download/v1/a.exe")
    with pytest.raises(HTTPError):
        handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/a.exe")
    with pytest.raises(HTTPError):
        handler.redirect_request(request, None, 302, "Found", {}, "http://objects.githubusercontent.com/a.exe")
    allowed = handler.redirect_request(
        request, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/a.exe?signature=1"
    )
    assert allowed is not None and allowed.full_url.startswith("https://release-assets.githubusercontent.com/")


def test_install_supported_requires_windows_worker_and_opt_in(tmp_path, monkeypatch):
    install = tmp_path / "install"
    app = install / "releases" / "0.2.0" / "app"
    (install / "releases").mkdir(parents=True)
    app.mkdir(parents=True)
    service = UpdateService(tmp_path / "runtime", role="worker", app_root=app, machine="AMD64")
    monkeypatch.setattr("menu_capture.updates.updates.sys.platform", "win32")
    monkeypatch.delenv("MENU_CAPTURE_ALLOW_SELF_UPDATE", raising=False)

    assert service.install_supported() is False
    assert service.status_no_check()["capabilities"]["install"] is False

    monkeypatch.setenv("MENU_CAPTURE_ALLOW_SELF_UPDATE", "1")
    assert service.install_supported() is True
    assert service.status_no_check()["capabilities"]["install"] is True

    hub = UpdateService(tmp_path / "hub", role="hub", app_root=app, machine="AMD64")
    assert hub.install_supported() is False



def _win_install(tmp_path: Path, monkeypatch, versions: list[str]) -> tuple[Path, Path]:
    """构造 Windows 安装布局：<install>/releases/<version>/app + 发布清单。"""
    install = tmp_path / "install"
    current_app = install / "releases" / versions[0] / "app"
    current_app.mkdir(parents=True)
    for version in versions:
        (install / "releases" / version / "app").mkdir(parents=True, exist_ok=True)
        (install / "releases" / version / "release-manifest.json").write_text(
            json.dumps({"version": version, "built_at": "2026-01-01", "git_commit": "c" * 40}),
            encoding="utf-8",
        )
    (install / "current-version").write_text(versions[0], encoding="utf-8")
    monkeypatch.setenv("MENU_CAPTURE_VERSION", versions[0])
    monkeypatch.setattr("menu_capture.updates.updates.sys.platform", "win32")
    return install, current_app


def test_inventory_reads_cached_sizes_without_rescanning(tmp_path, monkeypatch):
    # 版本目录体积遍历在 HDD 上要数分钟：状态接口只读缓存，命中时不得再次扫描。
    install, current_app = _win_install(tmp_path, monkeypatch, ["0.3.0", "0.3.1"])
    (install / "releases" / "0.3.0" / "app" / "blob.bin").write_bytes(b"x" * 256)
    service = UpdateService(tmp_path / "runtime", role="worker", app_root=current_app, machine="AMD64")

    first = service.inventory()
    assert first["sizes_pending"] is True
    assert all(row["size_bytes"] is None for row in first["releases"])
    _join_release_size_scan(service)

    def _fail(_directory):
        raise AssertionError("命中缓存时不应重新扫描版本目录")

    monkeypatch.setattr("menu_capture.updates.updates._tree_size", _fail)
    # 新进程实例从持久缓存恢复，说明缓存跨服务重启有效。
    again = UpdateService(
        tmp_path / "runtime", role="worker", app_root=current_app, machine="AMD64"
    ).inventory()
    assert again["sizes_pending"] is False
    sizes = {row["version"]: row["size_bytes"] for row in again["releases"]}
    assert sizes["0.3.0"] >= 256
    assert sizes["0.3.1"] is not None


def test_inventory_scans_new_release_and_keeps_old_cached(tmp_path, monkeypatch):
    install, current_app = _win_install(tmp_path, monkeypatch, ["0.3.0"])
    service = UpdateService(tmp_path / "runtime", role="worker", app_root=current_app, machine="AMD64")
    service.inventory()
    _join_release_size_scan(service)

    # 安装新版本只新增目录：旧版本继续命中缓存，新目录才进入后台统计。
    new_root = install / "releases" / "0.3.1"
    (new_root / "app").mkdir(parents=True)
    (new_root / "release-manifest.json").write_text(
        json.dumps({"version": "0.3.1", "built_at": "2026-02-01", "git_commit": "e" * 40}),
        encoding="utf-8",
    )
    (new_root / "app" / "blob.bin").write_bytes(b"y" * 64)

    pending = service.inventory()
    rows = {row["version"]: row["size_bytes"] for row in pending["releases"]}
    assert pending["sizes_pending"] is True
    assert rows["0.3.0"] is not None
    assert rows["0.3.1"] is None
    _join_release_size_scan(service)
    rows = {row["version"]: row["size_bytes"] for row in service.inventory()["releases"]}
    assert rows["0.3.1"] >= 64


def test_cleanup_previews_then_deletes_only_unprotected_releases(tmp_path, monkeypatch):
    # 保护规则优先于数量：运行中/当前 + 最近两个版本，共 3 个。
    install, current_app = _win_install(tmp_path, monkeypatch, ["0.3.0", "0.2.0", "0.1.9", "0.1.0"])
    (install / "releases" / "0.1.0" / "app" / "blob.bin").write_bytes(b"x" * 128)
    service = UpdateService(tmp_path / "runtime", role="worker", app_root=current_app, machine="AMD64")

    preview = service.storage_preview()
    release_scope = preview["scopes"]["releases"]
    assert [item["version"] for item in release_scope["items"]] == ["0.1.0"]
    # 预览不阻塞扫描：首次未命中时体积为空并标记 pending，只累加已知值。
    assert release_scope["sizes_pending"] is True
    assert release_scope["reclaimable_bytes"] == 0
    # 预览绝不删除文件。
    assert (install / "releases" / "0.1.0").is_dir()

    # 后台统计完成后体积来自缓存，再次预览必须命中且不再遍历目录。
    _join_release_size_scan(service)
    release_scope = service.storage_preview()["scopes"]["releases"]
    assert release_scope["sizes_pending"] is False
    assert release_scope["reclaimable_bytes"] >= 128

    with pytest.raises(ValueError):
        service.cleanup({})
    with pytest.raises(ValueError):
        service.cleanup({"confirm": "cleanup", "scopes": ["unknown"]})

    result = service.cleanup({"confirm": "cleanup", "scopes": ["releases"]})
    assert [row["version"] for row in result["deleted"]] == ["0.1.0"]
    assert result["freed_bytes"] >= 128
    assert not (install / "releases" / "0.1.0").exists()
    assert (install / "releases" / "0.2.0").is_dir()
    assert (install / "releases" / "0.3.0").is_dir()
    # current-version 指向的版本绝不能被改动。
    assert (install / "current-version").read_text(encoding="utf-8") == "0.3.0"
    assert any(row["action"] == "cleanup" for row in service.history())


def test_cleanup_blocked_while_install_active(tmp_path, monkeypatch):
    install, current_app = _win_install(tmp_path, monkeypatch, ["0.3.0", "0.2.0", "0.1.9", "0.1.0"])
    service = UpdateService(tmp_path / "runtime", role="worker", app_root=current_app, machine="AMD64")
    service.cleanup_guard = lambda: (True, "安装进行中")

    assert service.storage_preview()["blocked"] is True
    with pytest.raises(ValueError, match="安装进行中"):
        service.cleanup({"confirm": "cleanup"})
    assert (install / "releases" / "0.1.0").is_dir()


def test_cleanup_downloads_keeps_pending_install_artifact(tmp_path):
    service = UpdateService(tmp_path, role="worker", platform="windows", machine="AMD64")
    for version in ["0.2.0", "0.3.0"]:
        directory = service.download_dir / version
        directory.mkdir(parents=True)
        (directory / f"MenuCapture-{version}.exe").write_bytes(b"x" * 32)
    service._download_state = {
        "status": "completed", "version": "0.3.0", "asset_name": "MenuCapture-0.3.0.exe",
    }

    preview = service.storage_preview()
    assert [item["version"] for item in preview["scopes"]["downloads"]["items"]] == ["0.2.0"]
    assert preview["scopes"]["downloads"]["kept"] == ["0.3.0"]

    result = service.cleanup({"confirm": "cleanup", "scopes": ["downloads"]})
    assert [row["version"] for row in result["deleted"]] == ["0.2.0"]
    assert not (service.download_dir / "0.2.0").exists()
    assert (service.download_dir / "0.3.0" / "MenuCapture-0.3.0.exe").is_file()


def test_cleanup_logs_only_prunes_old_cli_transcripts(tmp_path):
    service = UpdateService(tmp_path, role="worker", platform="windows", machine="AMD64")
    cli_dir = service.data_dir / "debug-logs" / "cli"
    cli_dir.mkdir(parents=True)
    old = cli_dir / "old.log"
    recent = cli_dir / "recent.log"
    old.write_text("old", encoding="utf-8")
    recent.write_text("recent", encoding="utf-8")
    stale = time.time() - 8 * 24 * 3600
    os.utime(old, (stale, stale))

    preview = service.storage_preview()
    assert [item["name"] for item in preview["scopes"]["logs"]["items"]] == ["old.log"]

    result = service.cleanup({"confirm": "cleanup", "scopes": ["logs"]})
    assert [row["name"] for row in result["deleted"]] == ["old.log"]
    assert not old.exists()
    assert recent.exists()


def test_cleanup_refuses_symlinked_release_directory(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("当前平台不支持符号链接")
    install = tmp_path / "install"
    releases = install / "releases"
    releases.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    try:
        os.symlink(outside, releases / "0.1.0")
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")

    with pytest.raises(ValueError):
        UpdateService._remove_release(install, "0.1.0")
    assert (outside / "keep.txt").is_file()


# ---------- 自动下载、空闲后更新与保留数量（Issue #32 生命周期） ----------

def test_preferences_default_auto_download_off_and_keep_three(tmp_path):
    service = UpdateService(tmp_path, role="worker", platform="windows", machine="AMD64")

    preferences = service.status_no_check()["preferences"]

    assert preferences["auto_check"] is True
    assert preferences["auto_download"] is False
    assert preferences["keep_versions"] == 3


def test_preferences_validate_auto_download_and_keep_versions(tmp_path):
    service = UpdateService(tmp_path, role="worker", platform="windows", machine="AMD64")

    saved = service.save_preferences({"auto_download": True, "keep_versions": 5})

    assert saved["auto_download"] is True
    assert saved["keep_versions"] == 5
    # 重新加载仍保留；非法值不落盘。
    reloaded = UpdateService(tmp_path, role="worker", platform="windows", machine="AMD64")
    assert reloaded.status_no_check()["preferences"]["keep_versions"] == 5
    for payload in ({"keep_versions": 1}, {"keep_versions": 6}, {"keep_versions": True},
                    {"auto_download": "yes"}, {"unknown": 1}):
        with pytest.raises(ValueError):
            service.save_preferences(payload)


def test_keep_versions_extends_protected_releases(tmp_path, monkeypatch):
    install, current_app = _win_install(tmp_path, monkeypatch, ["0.5.0", "0.4.0", "0.3.0", "0.2.0", "0.1.0"])
    service = UpdateService(tmp_path / "runtime", role="worker", app_root=current_app, machine="AMD64")
    service.save_preferences({"keep_versions": 4})

    preview = service.storage_preview()["scopes"]["releases"]

    # 保护 0.5/0.4/0.3/0.2，只剩 0.1.0 可清理。
    assert [item["version"] for item in preview["items"]] == ["0.1.0"]
    assert service.inventory()["keep_versions"] == 4


def test_auto_download_starts_after_check_when_enabled(tmp_path):
    body = b"auto" * 500
    payload = download_manifest("0.3.0", body)
    rel = download_release("v0.3.0", payload, body)
    opener = FakeOpener(body)
    service = make_service(tmp_path, [rel], {"v0.3.0": payload}, opener=opener)
    service.save_preferences({"auto_download": True})

    service.check(force=True)

    state = wait_download(service)
    assert state["status"] == "completed"
    assert state["version"] == "0.3.0"


def test_auto_download_disabled_keeps_download_idle(tmp_path):
    body = b"manual" * 500
    payload = download_manifest("0.3.0", body)
    rel = download_release("v0.3.0", payload, body)
    service = make_service(tmp_path, [rel], {"v0.3.0": payload}, opener=FakeOpener(body))

    service.check(force=True)

    assert service.download_status()["status"] == "idle"


def _prepared_completed_download(tmp_path: Path) -> tuple[UpdateService, str]:
    body = b"sched" * 400
    service = prepared_service(tmp_path, body, FakeOpener(body))
    service.start_download()
    state = wait_download(service)
    assert state["status"] == "completed"
    return service, state["version"]


def test_schedule_install_requires_completed_download(tmp_path):
    service = prepared_service(tmp_path, b"not-downloaded" * 100, FakeOpener(b"x" * 100))
    service.install_capability = lambda: True
    service.schedule_installer = lambda version: {"status": "starting"}

    with pytest.raises(ValueError, match="请先下载更新"):
        service.schedule_install()


def test_schedule_install_arms_and_cancel_disarms(tmp_path):
    service, version = _prepared_completed_download(tmp_path)
    service.install_capability = lambda: True
    service.schedule_installer = lambda target: {"status": "starting"}

    armed = service.schedule_install()
    assert armed["armed"] is True
    assert armed["version"] == version
    assert service.status_no_check()["schedule"]["armed"] is True

    cancelled = service.cancel_install_schedule()
    assert cancelled["armed"] is False
    assert service.status_no_check()["schedule"]["armed"] is False
    assert any(row["action"] == "schedule" for row in service.history())


def test_schedule_monitor_retries_while_busy_then_starts(tmp_path, monkeypatch):
    import menu_capture.updates.updates as updates_module

    service, version = _prepared_completed_download(tmp_path)
    service.install_capability = lambda: True
    calls: list[str] = []

    def installer(target: str) -> dict:
        calls.append(target)
        if len(calls) < 3:
            raise ValueError("采集或人工标注正在执行，请先结束或暂停后再安装")
        return {"status": "starting"}

    service.schedule_installer = installer
    # 先缩短轮询间隔再启动监控，避免线程已按默认间隔等待。
    monkeypatch.setattr(updates_module, "SCHEDULE_POLL_SECONDS", 0.01)
    service.schedule_install()

    thread = service._schedule_thread
    assert thread is not None
    thread.join(timeout=5)

    assert calls == [version, version, version]
    schedule = service.install_schedule()
    assert schedule["armed"] is False
    assert schedule["last_result"] == "started"


def test_schedule_monitor_disarms_on_permanent_error(tmp_path, monkeypatch):
    import menu_capture.updates.updates as updates_module

    service, _ = _prepared_completed_download(tmp_path)
    service.install_capability = lambda: True

    def installer(_: str) -> dict:
        raise ValueError("更新包与当前发布清单不一致，请重新检查更新并下载")

    service.schedule_installer = installer
    # 先缩短轮询间隔再启动监控，避免线程已按默认间隔等待。
    monkeypatch.setattr(updates_module, "SCHEDULE_POLL_SECONDS", 0.01)
    service.schedule_install()

    thread = service._schedule_thread
    assert thread is not None
    thread.join(timeout=5)

    schedule = service.install_schedule()
    assert schedule["armed"] is False
    assert "不一致" in schedule["last_result"]


# ---------- 一次授权完成下载、等待、安装与重连（Issue #32 收敛后） ----------

def _flow_service(tmp_path: Path) -> UpdateService:
    body = b"flow" * 400
    service = prepared_service(tmp_path, body, FakeOpener(body))
    service.install_capability = lambda: True
    return service


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("条件未在超时内满足")


def test_authorize_update_downloads_then_installs_without_extra_confirm(tmp_path, monkeypatch):
    import menu_capture.updates.updates as updates_module

    service = _flow_service(tmp_path)
    calls: list[str] = []
    service.schedule_installer = lambda version: calls.append(version) or {"status": "starting"}
    monkeypatch.setattr(updates_module, "SCHEDULE_POLL_SECONDS", 0.01)

    flow = service.authorize_update({"mode": "immediate"})

    assert flow["version"] == "0.3.0"
    assert flow["active"] is True
    thread = service._schedule_thread
    assert thread is not None
    thread.join(timeout=5)
    assert calls == ["0.3.0"]
    assert service.download_status()["status"] == "completed"
    schedule = service.install_schedule()
    assert schedule["armed"] is False
    assert schedule["last_result"] == "started"


def test_authorize_update_joins_existing_schedule_idempotently(tmp_path):
    service = _flow_service(tmp_path)
    service.schedule_installer = lambda version: {"status": "starting"}

    service.start_download()
    assert wait_download(service)["status"] == "completed"
    first = service.authorize_update({"mode": "idle"})
    requested_at = service.install_schedule()["requested_at"]
    second = service.authorize_update({"mode": "idle"})

    assert first["version"] == second["version"] == "0.3.0"
    assert service.install_schedule()["requested_at"] == requested_at


def test_authorize_update_requires_install_capability(tmp_path):
    service = prepared_service(tmp_path, b"cap" * 300, FakeOpener(b"cap" * 300))

    with pytest.raises(ValueError, match="不支持应用内安装"):
        service.authorize_update({"mode": "immediate"})
    assert service.install_schedule()["armed"] is False


def test_authorize_update_rejects_unknown_mode(tmp_path):
    service = _flow_service(tmp_path)
    service.schedule_installer = lambda version: {"status": "starting"}

    with pytest.raises(ValueError, match="更新方式无效"):
        service.authorize_update({"mode": "later"})


def test_cancel_update_disarms_and_stops_download(tmp_path):
    service = _flow_service(tmp_path)
    service.schedule_installer = lambda version: {"status": "starting"}
    # 模拟同版本下载已在进行，避免真实网络线程让用例变慢。
    service._downloading = True
    service._download_state = {**service._download_state, "version": "0.3.0", "active": True}
    service._download_cancel = threading.Event()

    service.authorize_update({"mode": "idle"})
    flow = service.cancel_update()

    assert flow["stage"] != "installing"
    assert service.install_schedule()["armed"] is False
    assert service._download_cancel.is_set()


def test_cancel_update_rejected_after_install_started(tmp_path):
    service = _flow_service(tmp_path)
    service.install_snapshot_provider = lambda: {
        "status": "installing", "target_version": "0.3.0", "active": True,
    }

    with pytest.raises(ValueError, match="无法取消"):
        service.cancel_update()


def test_update_flow_reports_completed_only_for_authorized_version(tmp_path):
    service = _flow_service(tmp_path)
    service._schedule = {**service._schedule, "version": "0.3.0", "mode": "immediate", "armed": False}
    service.install_snapshot_provider = lambda: {
        "status": "completed", "target_version": "0.3.0", "active": False,
        "message": "安装完成，健康检查通过", "finished_at": "2026-11-25T00:00:00+00:00",
    }

    flow = service.update_flow()

    assert flow["stage"] == "completed"
    assert flow["active"] is False
    # 其他版本的旧结果不能套在当前授权上。
    service.install_snapshot_provider = lambda: {"status": "completed", "target_version": "0.2.9"}
    assert service.update_flow()["stage"] == "idle"


def test_update_flow_reports_waiting_reason_while_busy(tmp_path):
    service = _flow_service(tmp_path)
    service._schedule = {
        **service._schedule, "armed": True, "version": "0.3.0", "mode": "idle",
        "note": "采集或人工标注正在执行，请先结束或暂停后再安装",
    }

    flow = service.update_flow()

    assert flow["stage"] == "waiting"
    assert flow["active"] is True
    assert "采集" in flow["message"]


# ---------- 锁契约回归（Issue #32 OTA ABBA 死锁） ----------

def _lock_free_from_other_thread(lock, timeout: float = 2.0) -> bool:
    """必须换线程判断占用：RLock 可重入，同线程 acquire(blocking=False) 永远成功。"""
    outcome: dict[str, bool] = {}

    def probe() -> None:
        acquired = lock.acquire(timeout=timeout)
        outcome["free"] = acquired
        if acquired:
            lock.release()

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join(timeout + 1)
    return bool(outcome.get("free"))


def test_update_flow_reads_install_snapshot_outside_service_lock(tmp_path):
    """回归：持更新锁读取安装快照会与安装启动交叉持锁，形成 ABBA 死锁。"""
    service = _flow_service(tmp_path)
    seen: dict[str, bool] = {}

    def snapshot() -> dict:
        seen["lock_free"] = _lock_free_from_other_thread(service._lock)
        return {"status": "idle"}

    service.install_snapshot_provider = snapshot

    service.update_flow()

    assert seen["lock_free"] is True


def test_authorize_and_cancel_read_install_snapshot_outside_service_lock(tmp_path):
    service = _flow_service(tmp_path)
    seen: list[bool] = []

    def snapshot() -> dict:
        seen.append(_lock_free_from_other_thread(service._lock))
        return {"status": "idle"}

    service.install_snapshot_provider = snapshot
    service.schedule_installer = lambda version: {}
    # 没有可更新版本时授权会在取锁后立即失败，但快照必须已在锁外读过。
    service._cache = {"checked_at": 0, "latest": None}

    with pytest.raises(ValueError, match="没有可用的新版本"):
        service.authorize_update({"mode": "immediate"})
    service.cancel_update()

    assert seen and all(seen)


def test_cleanup_does_not_hold_state_lock_while_deleting(tmp_path, monkeypatch):
    """回归：清理会读取安装执行器状态并遍历删除大目录，不能占用全局状态锁。"""
    install, current_app = _win_install(tmp_path, monkeypatch, ["0.3.0", "0.2.0", "0.1.9", "0.1.0"])
    service = UpdateService(tmp_path / "runtime", role="worker", app_root=current_app, machine="AMD64")
    seen: dict[str, bool] = {}

    def guard() -> tuple[bool, str]:
        seen["lock_free"] = _lock_free_from_other_thread(service._lock)
        return False, ""

    service.cleanup_guard = guard

    result = service.cleanup({"confirm": "cleanup", "scopes": ["releases"]})

    assert seen["lock_free"] is True
    assert [row["version"] for row in result["deleted"]] == ["0.1.0"]


def test_cleanup_rechecks_install_state_without_taking_install_lock(tmp_path, monkeypatch):
    """清理在持文件系统锁期间只能用锁外探测；探测为活动时必须拒绝删除。"""
    install, current_app = _win_install(tmp_path, monkeypatch, ["0.3.0", "0.2.0", "0.1.9", "0.1.0"])
    service = UpdateService(tmp_path / "runtime", role="worker", app_root=current_app, machine="AMD64")
    calls: list[str] = []

    def guard() -> tuple[bool, str]:
        # 第一次（锁外）放行，第二次由文件系统锁内的只读探测接管。
        calls.append("guard")
        return False, ""

    service.cleanup_guard = guard
    service.install_active_provider = lambda: True

    with pytest.raises(ValueError, match="安装或回滚进行中"):
        service.cleanup({"confirm": "cleanup", "scopes": ["releases"]})

    assert calls == ["guard"]


def test_update_flow_surfaces_failure_after_background_disarms(tmp_path):
    service = _flow_service(tmp_path)
    service._schedule = {
        **service._schedule, "armed": False, "version": "0.3.0", "mode": "immediate",
        "last_result": "下载未完成：网络中断",
    }

    flow = service.update_flow()

    assert flow["stage"] == "failed"
    assert flow["error"] == "下载未完成：网络中断"
    # 用户取消后不应显示为失败。
    service._schedule = {**service._schedule, "last_result": "", "note": "已取消更新"}
    assert service.update_flow()["stage"] == "idle"


# ---------- 增量补丁清单与下载 ----------

PATCH_NAME = "MenuCapture-0.2.0-to-0.3.0.patch.zip"


def patch_payload(version: str, full_body: bytes, patch_body: bytes, *, catalog: str = "",
                  patch_name: str = PATCH_NAME) -> tuple[dict, dict]:
    payload = download_manifest(version, full_body)
    entry = {
        "from": "0.2.0",
        "name": patch_name,
        "size": len(patch_body),
        "sha256": hashlib.sha256(patch_body).hexdigest(),
        "url": f"{BASE}/v{version}/{patch_name}",
        "platform": "windows",
        "arch": "x64",
        "format": "zip-sha256-v1",
    }
    if catalog:
        entry["catalog"] = catalog
    payload["patches"] = [entry]
    return payload, entry


def release_with_patch(tag: str, payload: dict, full_body: bytes, patch_body: bytes,
                       *, patch_name: str = PATCH_NAME, extras: tuple[str, ...] = ()) -> dict:
    rel = download_release(tag, payload, full_body)
    rel["assets"].append({
        "name": patch_name,
        "size": len(patch_body),
        "browser_download_url": f"{BASE}/{tag}/{patch_name}",
        "digest": "sha256:" + hashlib.sha256(patch_body).hexdigest(),
    })
    for name in extras:
        rel["assets"].append({
            "name": name,
            "size": 5,
            "browser_download_url": f"{BASE}/{tag}/{name}",
            "digest": "",
        })
    return rel


def make_install_root(tmp_path: Path, version: str = "0.2.0", commit: str = "c" * 40) -> Path:
    root = tmp_path / "install"
    release_dir = root / "releases" / version
    release_dir.mkdir(parents=True)
    release_dir.joinpath("release-manifest.json").write_text(
        json.dumps({"version": version, "git_commit": commit}), encoding="utf-8"
    )
    return root


def test_manifest_accepts_optional_patch_extension(tmp_path):
    full, patch = b"full" * 10, b"patch" * 5
    payload, entry = patch_payload("0.3.0", full, patch, catalog="p.cat")
    rel = release_with_patch("v0.3.0", payload, full, patch, extras=("p.cat",))
    service = make_service(tmp_path, [rel], {"v0.3.0": payload})

    status = service.check(force=True)

    assert status["update_available"] is True
    patches = status["latest"]["patches"]
    assert len(patches) == 1
    assert patches[0]["from"] == entry["from"]
    assert patches[0]["name"] == entry["name"]
    assert patches[0]["catalog"] == "p.cat"
    assert patches[0]["kind"] == "windows_patch"


@pytest.mark.parametrize("mutation", [
    lambda entry: {**entry, "format": "zip-sha1-v1"},
    lambda entry: {**entry, "arch": "arm64"},
    lambda entry: {**entry, "from": "0.3.0"},
    lambda entry: {**entry, "size": 1},
    lambda entry: {**entry, "sha256": "f" * 64},
    lambda entry: {**entry, "url": f"{BASE}/v9.9.9/{entry['name']}"},
    lambda entry: {**entry, "name": "not-in-release.zip"},
])
def test_invalid_patch_entry_never_breaks_full_asset(tmp_path, mutation):
    full, patch = b"full" * 10, b"patch" * 5
    payload, entry = patch_payload("0.3.0", full, patch)
    payload["patches"] = [mutation(entry)]
    rel = release_with_patch("v0.3.0", payload, full, patch)
    service = make_service(tmp_path, [rel], {"v0.3.0": payload})

    status = service.check(force=True)

    assert status["update_available"] is True
    assert status["latest"]["patches"] == []


def test_malformed_patches_field_is_ignored(tmp_path):
    full = b"full" * 10
    payload = download_manifest("0.3.0", full)
    payload["patches"] = {"bad": "shape"}
    rel = download_release("v0.3.0", payload, full)
    service = make_service(tmp_path, [rel], {"v0.3.0": payload})

    status = service.check(force=True)

    assert status["update_available"] is True
    assert status["latest"]["patches"] == []


def _patch_entry() -> dict:
    return {
        "kind": "windows_patch",
        "from": "0.2.0",
        "name": PATCH_NAME,
        "size": 10,
        "sha256": "a" * 64,
        "url": f"{BASE}/v0.3.0/{PATCH_NAME}",
        "platform": "windows",
        "arch": "x64",
        "format": "zip-sha256-v1",
    }


def test_select_patch_requires_opt_in_and_trusted_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(updates_module.sys, "platform", "win32")
    root = make_install_root(tmp_path)
    service = UpdateService(
        tmp_path / "runtime", role="worker", platform="windows", machine="AMD64",
        install_root=root, fetcher=Fetcher([], {}),
    )
    latest = {"patches": [_patch_entry()]}

    # 默认关闭增量，即使基线与补丁都齐全。
    assert service._select_patch(latest, "0.2.0") is None

    monkeypatch.setenv("MENU_CAPTURE_ALLOW_INCREMENTAL_UPDATE", "1")
    # 没有签名 catalog 时，必须沿用既有 unsigned 放行开关。
    assert service._select_patch(latest, "0.2.0") is None

    monkeypatch.setenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", "1")
    assert service._select_patch(latest, "0.2.0")["name"] == PATCH_NAME

    # 签名 catalog 存在时不再要求 unsigned 放行。
    monkeypatch.delenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE")
    latest = {"patches": [{**_patch_entry(), "catalog": "p.cat"}]}
    assert service._select_patch(latest, "0.2.0")["catalog"] == "p.cat"

    # 基线清单版本不符时拒绝。
    root.joinpath("releases/0.2.0/release-manifest.json").write_text(
        json.dumps({"version": "0.1.9"}), encoding="utf-8"
    )
    assert service._select_patch(latest, "0.2.0") is None


def test_patch_download_preferred_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(updates_module.sys, "platform", "win32")
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_INCREMENTAL_UPDATE", "1")
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", "1")
    full, patch = b"exe" * 100, b"patch" * 20
    payload, _ = patch_payload("0.3.0", full, patch)
    rel = release_with_patch("v0.3.0", payload, full, patch)
    service = make_service(
        tmp_path, [rel], {"v0.3.0": payload}, opener=FakeOpener(patch),
        install_root=make_install_root(tmp_path),
    )
    service.check(force=True)

    service.start_download()
    state = wait_download(service)

    assert state["status"] == "completed"
    assert state["asset_kind"] == "windows_patch"
    assert state["from_version"] == "0.2.0"
    assert Path(state["path"]).read_bytes() == patch


class PatchFailOpener(FakeOpener):
    def __call__(self, url, headers, timeout):
        if url.endswith(PATCH_NAME):
            raise OSError("patch unavailable")
        return super().__call__(url, headers, timeout)


def test_patch_download_failure_falls_back_to_full_once(tmp_path, monkeypatch):
    monkeypatch.setattr(updates_module.sys, "platform", "win32")
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_INCREMENTAL_UPDATE", "1")
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", "1")
    full, patch = b"exe" * 100, b"patch" * 20
    payload, _ = patch_payload("0.3.0", full, patch)
    rel = release_with_patch("v0.3.0", payload, full, patch)
    service = make_service(
        tmp_path, [rel], {"v0.3.0": payload}, opener=PatchFailOpener(full),
        install_root=make_install_root(tmp_path),
    )
    service.check(force=True)

    service.start_download()
    state = wait_download(service)

    assert state["status"] == "completed"
    assert state["asset_kind"] == "windows_installer"
    assert state["fallback_reason"]
    assert Path(state["path"]).read_bytes() == full


def test_cancelled_patch_download_does_not_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(updates_module.sys, "platform", "win32")
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_INCREMENTAL_UPDATE", "1")
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", "1")
    monkeypatch.setattr(updates_module, "DOWNLOAD_CHUNK_BYTES", 8)
    full, patch = b"exe" * 100, b"patch" * 200
    payload, _ = patch_payload("0.3.0", full, patch)
    rel = release_with_patch("v0.3.0", payload, full, patch)
    opener = GatedOpener(patch)
    service = make_service(
        tmp_path, [rel], {"v0.3.0": payload}, opener=opener,
        install_root=make_install_root(tmp_path),
    )
    service.check(force=True)

    service.start_download()
    deadline = time.monotonic() + 5
    while service.download_status()["downloaded"] == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    service.cancel_download()
    # 放行被阻塞的读取，让下载线程回到循环顶部观察取消，避免依赖 5 秒门超时。
    opener.gate.set()
    state = wait_download(service)

    assert state["status"] == "cancelled"
    assert state["asset_kind"] == "windows_patch"
    assert state["fallback_reason"] == ""


def test_hub_manifest_accepts_pinned_oci_extension(tmp_path: Path):
    service = make_hub_service(tmp_path, hub_manifest())

    status = service.check(force=True)

    asset = status["latest"]["assets"][0]
    assert asset["registry"] == OCI_REGISTRY
    assert asset["digest"] == OCI_DIGEST
    assert status["update_available"] is True


@pytest.mark.parametrize("registry,digest", [
    ("docker.io/owner/repo", OCI_DIGEST),
    (OCI_REGISTRY, "sha256:" + "d" * 63),
])
def test_hub_manifest_drops_invalid_oci_without_losing_archive(tmp_path: Path, registry: str, digest: str):
    service = make_hub_service(tmp_path, hub_manifest(registry=registry, digest=digest))

    status = service.check(force=True)

    asset = status["latest"]["assets"][0]
    assert "registry" not in asset and "digest" not in asset
    assert asset["sha256"] == "c" * 64 and asset["size"] == 2048
    assert status["update_available"] is True


def test_oci_mode_refuses_full_archive_download(tmp_path: Path):
    service = make_hub_service(tmp_path, hub_manifest())
    service.check(force=True)
    service.oci_delivery_provider = lambda: True

    with pytest.raises(ValueError, match="无需下载"):
        service.start_download()

    assert service.download_status()["status"] == "idle"


def test_oci_authorize_skips_archive_download_and_starts_installer(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(updates_module, "SCHEDULE_POLL_SECONDS", 0.01)
    service = make_hub_service(tmp_path, hub_manifest())
    service.check(force=True)
    service.install_capability = lambda: True
    service.oci_delivery_provider = lambda: True
    calls: list[str] = []
    service.schedule_installer = lambda version: calls.append(version) or {"status": "starting"}

    flow = service.authorize_update({"mode": "immediate"})

    assert flow["active"] is True
    assert flow["stage"] == "waiting"
    assert flow["message"] == "等待空闲，由宿主机分层拉取镜像"
    deadline = time.monotonic() + 2
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == ["0.3.0"]
    assert service.download_status()["status"] == "idle"
    assert not (service.download_dir / "0.3.0").exists()


def test_schedule_install_allowed_without_local_artifact_in_oci_mode(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(updates_module, "SCHEDULE_POLL_SECONDS", 0.05)
    service = make_hub_service(tmp_path, hub_manifest())
    service.check(force=True)
    service.install_capability = lambda: True
    service.oci_delivery_provider = lambda: True
    service.schedule_installer = lambda version: {"status": "starting"}

    schedule = service.schedule_install()

    assert schedule["armed"] is True
    assert schedule["version"] == "0.3.0"


def test_oci_capability_loss_falls_back_to_archive_download_once(tmp_path: Path, monkeypatch):
    """授权后宿主能力消失时，同一次授权内回退一次完整下载并继续安装。"""
    monkeypatch.setattr(updates_module, "SCHEDULE_POLL_SECONDS", 0.01)
    service = make_hub_service(tmp_path, hub_manifest())
    service.check(force=True)
    service.install_capability = lambda: True
    capable = {"enabled": True}
    service.oci_delivery_provider = lambda: capable["enabled"]
    service._downloaded_artifact_ready = lambda version: True
    started: list[str] = []
    service.schedule_installer = lambda version: started.append(version) or {"status": "starting"}
    downloads: list[int] = []

    def fake_start_download():
        downloads.append(1)
        with service._lock:
            service._download_state = {
                **service._download_state,
                "status": "completed",
                "version": "0.3.0",
                "asset_name": "hub-image-0.3.0.tar.gz",
                "sha256": "c" * 64,
                "total": 2048,
            }
        return service.download_status()

    service.start_download = fake_start_download
    flow = service.authorize_update({"mode": "immediate"})
    assert flow["message"] == "等待空闲，由宿主机分层拉取镜像"

    capable["enabled"] = False
    deadline = time.monotonic() + 2
    while not started and time.monotonic() < deadline:
        time.sleep(0.01)

    # 归档下载从未启动，必须回退一次；随后按正常下载完成路径开始安装，而不是永久等待。
    assert downloads == [1]
    assert started == ["0.3.0"]
    assert service.install_schedule()["armed"] is False


def test_oci_capability_loss_without_archive_disarms_explicitly(tmp_path: Path, monkeypatch):
    """无法回退完整下载时必须明确结束，不能无限等待未启动的归档下载。"""
    monkeypatch.setattr(updates_module, "SCHEDULE_POLL_SECONDS", 0.01)
    service = make_hub_service(tmp_path, hub_manifest())
    service.check(force=True)
    service.install_capability = lambda: True
    capable = {"enabled": True}
    service.oci_delivery_provider = lambda: capable["enabled"]
    started: list[str] = []
    service.schedule_installer = lambda version: started.append(version) or {"status": "starting"}
    downloads: list[int] = []

    def fail_download():
        downloads.append(1)
        raise ValueError("当前没有可下载的更新")

    service.start_download = fail_download
    service.authorize_update({"mode": "immediate"})

    capable["enabled"] = False
    deadline = time.monotonic() + 2
    while service.install_schedule()["armed"] and time.monotonic() < deadline:
        time.sleep(0.01)
    schedule = service.install_schedule()

    assert downloads == [1]
    assert started == []
    assert schedule["armed"] is False
    assert "无法回退完整下载" in schedule["last_result"]


def test_update_flow_treats_host_executor_pulling_as_installing(tmp_path: Path):
    service = make_hub_service(tmp_path, hub_manifest())
    service.check(force=True)
    service.install_capability = lambda: True
    service.install_snapshot_provider = lambda: {
        "status": "pulling", "target_version": "0.3.0", "message": "正在分层拉取镜像",
    }
    service._schedule = {**service._schedule, "armed": False, "version": "0.3.0"}

    flow = service.update_flow()

    assert flow["active"] is True
    assert flow["stage"] == "installing"
    assert flow["message"] == "正在分层拉取镜像"
    with pytest.raises(ValueError, match="安装已开始"):
        service.cancel_update()
