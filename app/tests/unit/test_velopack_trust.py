from __future__ import annotations

import base64
import hashlib
from copy import deepcopy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from menu_capture.updates import velopack_trust as trust
from menu_capture.updates.velopack_identity import validate_extension


REPOSITORY = "owner/repo"
VERSION = "0.4.0"
COMMIT = "a" * 40
TAG = f"v{VERSION}"


def _asset(name: str) -> dict:
    body = name.encode("utf-8")
    return {
        "name": name,
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "url": f"https://github.com/{REPOSITORY}/releases/download/{TAG}/{name}",
    }


def _manifest() -> dict:
    return {
        "version": VERSION,
        "channel": "stable",
        "git_commit": COMMIT,
        "built_at": "2026-11-26T00:00:00Z",
        "min_supported_version": "0.3.0",
        "rollback": "supported",
        "assets": [{
            "kind": "hub_image", "name": "hub.tar.gz", "size": 100,
            "sha256": "b" * 64,
            "url": f"https://github.com/{REPOSITORY}/releases/download/{TAG}/hub.tar.gz",
            "platform": "linux", "arch": "amd64",
            "registry": "ghcr.io/owner/repo", "digest": "sha256:" + "c" * 64,
        }],
        "velopack": {
            "schema_version": 1,
            "pack_id": "Yipu.MenuCapture",
            "platform": "windows",
            "arch": "x64",
            "channel": "win-x64-stable",
            "version": VERSION,
            "git_commit": COMMIT,
            "tag": TAG,
            "setup": _asset("MenuCaptureSetup.exe"),
            "full": _asset("MenuCapture-full.nupkg"),
            "deltas": [_asset("MenuCapture-delta.nupkg")],
            "index": _asset("releases.win-x64-stable.json"),
        },
    }


@pytest.fixture
def signing_key(monkeypatch):
    # 临时密钥仅供测试；永不写入项目配置或生产信任根。
    private = Ed25519PrivateKey.from_private_bytes(b"\x17" * 32)
    seed = private.private_bytes_raw()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode("ascii")
    key_id = trust.key_id_for(public)
    monkeypatch.setattr(trust, "PUBLISHER_KEYS", {key_id: public})
    return seed, public, key_id


def _signed_manifest(signing_key) -> dict:
    seed, _, _ = signing_key
    return trust.sign_manifest(_manifest(), seed)


def test_signed_policy_round_trips_through_extension_validation(signing_key):
    manifest = _signed_manifest(signing_key)
    policy = trust.release_policy_from_manifest(manifest)
    extension = validate_extension(
        manifest["velopack"], version=VERSION, commit=COMMIT, channel="stable",
        repository=REPOSITORY, tag=TAG, release_policy=policy,
    )
    assert extension["trust"]["mode"] == "ed25519"
    assert extension["release_policy"] == policy
    trust.verify_release(extension, release_policy=policy)


def test_signed_release_rejects_tampered_asset(signing_key):
    signed = _signed_manifest(signing_key)["velopack"]
    signed["full"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="签名验证失败"):
        trust.verify_release(signed)


def test_signed_release_rejects_missing_or_unknown_trust_root(signing_key, monkeypatch):
    signed = _signed_manifest(signing_key)["velopack"]
    with pytest.raises(ValueError, match="未知或信任根未配置"):
        trust.verify_release(signed, keys={})
    monkeypatch.setattr(trust, "PUBLISHER_KEYS", {})
    with pytest.raises(ValueError, match="未知或信任根未配置"):
        trust.verify_release(signed)


def test_signed_release_rejects_missing_signature(signing_key):
    signed = _signed_manifest(signing_key)["velopack"]
    del signed["trust"]["signature"]
    with pytest.raises(ValueError, match="签名缺失"):
        trust.verify_release(signed)


def test_outer_policy_tampering_is_rejected(signing_key):
    manifest = _signed_manifest(signing_key)
    changed = deepcopy(manifest)
    changed["min_supported_version"] = "0.4.0"
    with pytest.raises(ValueError, match="外层安全策略与签名内容不一致"):
        validate_extension(
            changed["velopack"], version=VERSION, commit=COMMIT, channel="stable",
            repository=REPOSITORY, tag=TAG,
            release_policy=trust.release_policy_from_manifest(changed),
        )


def test_embedded_policy_tampering_is_rejected(signing_key):
    signed = _signed_manifest(signing_key)["velopack"]
    signed["release_policy"]["rollback"] = "unsupported"
    with pytest.raises(ValueError, match="签名验证失败"):
        trust.verify_release(signed)


def test_signing_refuses_missing_secret(monkeypatch):
    monkeypatch.delenv("MENU_CAPTURE_RELEASE_SIGNING_KEY", raising=False)
    with pytest.raises(ValueError, match="缺少 MENU_CAPTURE_RELEASE_SIGNING_KEY"):
        trust.signing_key_from_env()


def test_signing_requires_full_manifest_and_pinned_key(signing_key, monkeypatch):
    seed, _, _ = signing_key
    with pytest.raises(ValueError, match="完整发布清单"):
        trust.sign_manifest(_manifest()["velopack"], seed)
    monkeypatch.setattr(trust, "PUBLISHER_KEYS", {})
    with pytest.raises(ValueError, match="固定信任根"):
        trust.sign_manifest(_manifest(), seed)


def test_component_independent_manifests_sign_and_isolate_updates(signing_key):
    from menu_capture.updates.updates import _update_available, _validate_manifest

    seed, _, _ = signing_key

    # 1. 仅 Hub 独立发布：无 velopack 字段，由顶层 Ed25519 trust 签名保护。
    hub_only = _manifest()
    del hub_only["velopack"]
    hub_only["components"] = ["hub"]
    signed_hub = trust.sign_manifest(hub_only, seed)
    assert "trust" in signed_hub and "velopack" not in signed_hub
    validated_hub = _validate_manifest(signed_hub, TAG, "stable", None, repository=REPOSITORY)
    validated_hub["eligible"] = True
    assert _update_available(validated_hub, "0.3.4", "linux", "x64", velopack=False) is True
    assert _update_available(validated_hub, "0.3.4", "windows", "x64", velopack=True) is False
    # 篡改仅 Hub 清单中的镜像摘要必须验签失败。
    tampered_hub = deepcopy(signed_hub)
    tampered_hub["assets"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="签名验证失败"):
        _validate_manifest(tampered_hub, TAG, "stable", None, repository=REPOSITORY)

    # 2. 仅 Windows 独立发布：assets 为空列表，由 velopack.trust 签名保护。
    win_only = _manifest()
    win_only["assets"] = []
    win_only["components"] = ["windows"]
    signed_win = trust.sign_manifest(win_only, seed)
    validated_win = _validate_manifest(signed_win, TAG, "stable", None, repository=REPOSITORY)
    validated_win["eligible"] = True
    assert _update_available(validated_win, "0.3.4", "windows", "x64", velopack=True) is True
    assert _update_available(validated_win, "0.3.4", "linux", "x64", velopack=False) is False
