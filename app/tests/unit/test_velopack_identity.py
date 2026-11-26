import hashlib
from copy import deepcopy
from types import SimpleNamespace

import pytest
from menu_capture.updates.velopack_identity import require_target, sdk_asset_identity, validate_extension, require_unsigned_opt_in

REPO = "owner/repo"
VERSION = "0.4.0"
COMMIT = "a" * 40
BODY = b"verified full package"


def fixture():
    def asset(name):
        return {"name": name, "size": len(BODY), "sha256": hashlib.sha256(BODY).hexdigest(),
                "url": f"https://github.com/{REPO}/releases/download/v{VERSION}/{name}"}
    return {"schema_version": 1, "pack_id": "Yipu.MenuCapture", "platform": "windows", "arch": "x64",
            "channel": "win-x64-stable", "version": VERSION, "git_commit": COMMIT, "tag": f"v{VERSION}",
            "setup": asset("Setup.exe"), "full": asset("full.nupkg"), "deltas": [asset("delta.nupkg")],
            "index": asset("releases.win-x64-stable.json"), "trust": {"mode": "unsigned"}}


def validate(target, published=None):
    return validate_extension(target, version=VERSION, commit=COMMIT, channel="stable", repository=REPO,
                              tag=f"v{VERSION}", published=published)


def test_valid():
    assert validate(fixture()) == fixture()


@pytest.mark.parametrize("field,value", [("pack_id", "evil"), ("git_commit", "b"*40),
                                           ("channel", "win-x64-preview"), ("trust", {"mode": "signed"})])
def test_bad_extension(field, value):
    target = fixture()
    target[field] = value
    with pytest.raises(ValueError):
        validate(target)


@pytest.mark.parametrize("field,value", [("name", "../full.nupkg"), ("size", True), ("sha256", "x"*64),
                                           ("url", "https://github.com/evil/repo/releases/download/v0.4.0/full.nupkg")])
def test_bad_asset(field, value):
    target = fixture()
    target["full"][field] = value
    with pytest.raises(ValueError):
        validate(target)


def test_github_identity():
    target = fixture()
    assets = [target["setup"], target["full"], *target["deltas"], target["index"]]
    published = {a["name"]: {"size": a["size"], "browser_download_url": a["url"],
                            "digest": "sha256:"+a["sha256"]} for a in assets}
    assert validate(target, published) == target
    published["full.nupkg"]["digest"] = "sha256:"+"0"*64
    with pytest.raises(ValueError):
        validate(target, published)


def test_sdk_identity_matches_target():
    # 包级完整性由官方 SDK 负责；这里只校验 SDK 目标与已签名发布身份一致。
    target = validate(fixture())
    full = target["full"]
    asset = SimpleNamespace(Version=VERSION, PackageId=target["pack_id"], Type="Full", FileName=full["name"],
                            SHA256=full["sha256"], Size=full["size"])
    require_target(sdk_asset_identity(asset), target)
    wrong = deepcopy(target)
    wrong["full"]["sha256"] = "0"*64
    with pytest.raises(ValueError):
        require_target(sdk_asset_identity(asset), wrong)


def test_unsigned_explicit_opt_in(monkeypatch):
    target = fixture()
    monkeypatch.delenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", raising=False)
    with pytest.raises(ValueError):
        require_unsigned_opt_in(target)
    monkeypatch.setenv("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE", "1")
    require_unsigned_opt_in(target)
    target["trust"] = {"mode": "invalid"}
    with pytest.raises(ValueError):
        require_unsigned_opt_in(target)
