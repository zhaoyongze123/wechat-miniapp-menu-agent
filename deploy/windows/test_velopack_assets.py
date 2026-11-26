import hashlib
import json
from pathlib import Path

import pytest

from velopack_assets import release_assets, release_channel


def fixture(root: Path, version: str = "0.4.0", *, delta: bool = True):
    names = ["Yipu.MenuCapture-Setup.exe", f"Yipu.MenuCapture-{version}-full.nupkg"]
    if delta:
        names.append(f"Yipu.MenuCapture-{version}-delta.nupkg")
    entries = []
    for i, name in enumerate(names):
        body = name.encode()
        (root / name).write_bytes(body)
        if i:
            entries.append({"PackageId": "Yipu.MenuCapture", "Version": version,
                            "Type": "Full" if i == 1 else "Delta", "FileName": name,
                            "Size": len(body), "SHA256": hashlib.sha256(body).hexdigest()})
    # 历史包可以只作为构建基线，不纳入本次发布资产。
    entries.append({"PackageId": "Yipu.MenuCapture", "Version": "0.3.9", "Type": "Full",
                    "FileName": "historical-full.nupkg"})
    index = root / f"releases.{release_channel(version)}.json"
    index.write_text(json.dumps({"Assets": entries}), encoding="utf-8")
    return names + [index.name], index, entries


@pytest.mark.parametrize("version", ["0.4.0", "0.4.0-rc.1"])
@pytest.mark.parametrize("delta", [True, False])
def test_release_uses_channel_and_real_index_names(tmp_path, version, delta):
    expected, _, _ = fixture(tmp_path, version, delta=delta)
    assert release_assets(tmp_path, version) == expected


def test_reject_wrong_channel(tmp_path):
    fixture(tmp_path, "0.4.0-rc.1")
    with pytest.raises(ValueError, match="缺少"):
        release_assets(tmp_path, "0.4.0")


@pytest.mark.parametrize("bad_name", ["../evil.nupkg", "..\\evil.nupkg", "C:evil.nupkg"])
def test_reject_path_traversal(tmp_path, bad_name):
    _, index, entries = fixture(tmp_path)
    entries[0]["FileName"] = bad_name
    index.write_text(json.dumps({"Assets": entries}), encoding="utf-8")
    with pytest.raises(ValueError, match="名称无效"):
        release_assets(tmp_path, "0.4.0")


def test_reject_changed_package(tmp_path):
    names, _, _ = fixture(tmp_path)
    (tmp_path / names[1]).write_bytes(b"x" * (tmp_path / names[1]).stat().st_size)
    with pytest.raises(ValueError, match="摘要不一致"):
        release_assets(tmp_path, "0.4.0")


def test_reject_duplicate_full(tmp_path):
    _, index, entries = fixture(tmp_path)
    entries.append(dict(entries[0]))
    index.write_text(json.dumps({"Assets": entries}), encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        release_assets(tmp_path, "0.4.0")


def test_sha1_only_index_is_supported(tmp_path):
    names, index, entries = fixture(tmp_path, delta=False)
    entries[0].pop("SHA256")
    entries[0]["SHA1"] = hashlib.sha1((tmp_path / names[1]).read_bytes()).hexdigest().upper()
    index.write_text(json.dumps({"Assets": entries}), encoding="utf-8")
    assert release_assets(tmp_path, "0.4.0") == names
