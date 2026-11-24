"""更新发现、清单校验、缓存与本地版本保护规则。"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from menu_capture.updates import CACHE_TTL_SECONDS, UpdateService, Version, is_newer


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


def make_service(tmp_path: Path, releases, manifests, *, current="0.2.0", channel="stable", fetcher=None):
    service = UpdateService(tmp_path, role="worker", fetcher=fetcher or Fetcher(releases, manifests))
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
    monkeypatch.setenv("MENU_CAPTURE_VERSION", "0.2.0")
    monkeypatch.setattr("menu_capture.updates.sys.platform", "win32")

    service = UpdateService(current_app, role="worker")
    inventory = service.inventory()

    assert inventory["supported"] is True
    protected = {row["version"] for row in inventory["releases"] if row["protected"]}
    assert protected == {"0.2.0", "0.1.9", "0.1.8"}
    assert [row["version"] for row in inventory["cleanup_candidates"]] == ["0.1.7"]
    assert inventory["cleanup_supported"] is False


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
