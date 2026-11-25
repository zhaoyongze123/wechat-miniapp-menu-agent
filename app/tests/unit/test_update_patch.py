from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from menu_capture.updates import update_patch
from menu_capture.updates.update_patch import PatchError, PatchScopeError


def write_tree(root: Path, files: dict[str, bytes]) -> Path:
    for name, content in files.items():
        path = root / Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


BASE = {
    "app/menu_capture/api/server.py": b"old-server",
    "app/ui_tars/business.mjs": b"same-module",
    "app/web/react/assets/index-abc.js": b"old-frontend",
    "app/web/react/index.css": b"old-css",
    "python/python.exe": b"runtime",
    "release-manifest.json": b'{"version": "0.3.1"}',
}
TARGET = {
    "app/menu_capture/api/server.py": b"new-server",
    "app/ui_tars/business.mjs": b"same-module",
    "app/web/react/assets/index-def.js": b"new-frontend",
    "python/python.exe": b"runtime",
    "release-manifest.json": b'{"version": "0.3.2"}',
}


def build_patch(tmp_path: Path, base=None, target=None, **kwargs) -> tuple[Path, dict]:
    base_dir = write_tree(tmp_path / "base", BASE if base is None else base)
    target_dir = write_tree(tmp_path / "target", TARGET if target is None else target)
    output = tmp_path / "patch.zip"
    metadata = update_patch.make_patch(
        base_dir, target_dir, from_version="0.3.1", to_version="0.3.2", output=output, **kwargs
    )
    assert metadata is not None
    return output, metadata


def test_make_and_apply_patch_rebuilds_exact_target(tmp_path):
    zip_path, metadata = build_patch(tmp_path)
    # server.py、前端入口与 release-manifest.json 都发生了变化，旧前端与旧样式被删除。
    assert metadata["changed"] == 3
    assert metadata["deleted"] == 2
    assert metadata["sha256"] == update_patch.file_sha256(zip_path)

    stage = tmp_path / "stage"
    result = update_patch.apply(
        tmp_path / "base", stage, zip_path, from_version="0.3.1", to_version="0.3.2"
    )
    assert result["changed"] == 3
    actual = update_patch.scan_tree(stage)
    expected = update_patch.scan_tree(tmp_path / "target")
    assert actual == expected
    assert not (stage / "app/web/react/assets/index-abc.js").exists()
    assert not (stage / "app/web/react/index.css").exists()


def test_make_patch_skips_when_python_runtime_changes(tmp_path):
    target = dict(TARGET)
    target["python/python.exe"] = b"runtime-2"
    base_dir = write_tree(tmp_path / "base", BASE)
    target_dir = write_tree(tmp_path / "target", target)
    assert update_patch.make_patch(
        base_dir, target_dir, from_version="0.3.1", to_version="0.3.2",
        output=tmp_path / "patch.zip",
    ) is None


def test_make_patch_skips_when_root_launch_script_changes(tmp_path):
    base_dir = write_tree(tmp_path / "base", BASE)
    target_dir = write_tree(tmp_path / "target", TARGET)
    (base_dir / "Launch.ps1").write_text("old", encoding="utf-8")
    (target_dir / "Launch.ps1").write_text("new", encoding="utf-8")
    assert update_patch.make_patch(
        base_dir, target_dir, from_version="0.3.1", to_version="0.3.2",
        output=tmp_path / "patch.zip",
    ) is None


def test_apply_rejects_corrupted_base_file(tmp_path):
    zip_path, _ = build_patch(tmp_path)
    # 未修改的基线文件被篡改：补丁无法据此重建可信目标树。
    (tmp_path / "base/app/ui_tars/business.mjs").write_bytes(b"tampered")
    with pytest.raises(PatchError, match="基线文件哈希不一致"):
        update_patch.apply(
            tmp_path / "base", tmp_path / "stage", zip_path,
            from_version="0.3.1", to_version="0.3.2",
        )
    assert not (tmp_path / "stage").exists()


def test_apply_rejects_existing_stage_directory(tmp_path):
    zip_path, _ = build_patch(tmp_path)
    (tmp_path / "stage").mkdir()
    with pytest.raises(PatchError, match="临时目录已存在"):
        update_patch.apply(
            tmp_path / "base", tmp_path / "stage", zip_path,
            from_version="0.3.1", to_version="0.3.2",
        )


@pytest.mark.parametrize("field,value,message", [
    ("from_version", "0.3.0", "版本"),
    ("to_version", "0.3.3", "版本"),
    ("platform", "linux", "平台"),
    ("arch", "arm64", "架构"),
    ("from_commit", "a" * 40, "基线提交"),
    ("to_commit", "b" * 40, "目标提交"),
])
def test_apply_rejects_binding_mismatch(tmp_path, field, value, message):
    zip_path, _ = build_patch(tmp_path)
    kwargs = {"from_version": "0.3.1", "to_version": "0.3.2", field: value}
    with pytest.raises(PatchError, match=message):
        update_patch.apply(tmp_path / "base", tmp_path / "stage", zip_path, **kwargs)


def _write_zip(tmp_path: Path, manifest: dict, files: dict[str, bytes] | None = None) -> Path:
    path = tmp_path / "crafted.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("patch-manifest.json", json.dumps(manifest))
        for name, content in (files or {}).items():
            archive.writestr(f"files/{name}", content)
    return path


def base_manifest(**overrides) -> dict:
    base_files = {name: hashlib.sha256(content).hexdigest() for name, content in BASE.items()}
    target_files = {name: hashlib.sha256(content).hexdigest() for name, content in TARGET.items()}
    manifest = {
        "format": "zip-sha256-v1",
        "from": "0.3.1",
        "to": "0.3.2",
        "from_commit": "",
        "to_commit": "",
        "platform": "windows",
        "arch": "x64",
        "scope": {"base": "b", "target": "b"},
        "base_files": base_files,
        "files": target_files,
        "changed": ["app/menu_capture/api/server.py"],
        "deleted": [],
    }
    manifest.update(overrides)
    return manifest


@pytest.mark.parametrize("path", [
    "../evil.py",
    "/absolute.py",
    "app\\server.py",
    "C:/windows/system32/evil.dll",
    "app/file.py:stream",
    "CON",
    "app/NUL.txt",
    "./app/server.py",
    "app/./server.py",
    "app/server.py.",
    "app/server .py",
])
def test_manifest_rejects_unsafe_paths(tmp_path, path):
    manifest = base_manifest(changed=[path])
    zip_path = _write_zip(tmp_path, manifest, {path: b"x"})
    with pytest.raises(PatchError):
        update_patch.load_patch(zip_path)


def test_manifest_rejects_case_collision(tmp_path):
    manifest = base_manifest()
    files = dict(manifest["files"])
    # Windows 文件系统不区分大小写，仅大小写不同的两个条目会互相覆盖。
    files["App/UI_Tars/Business.MJS"] = files["app/ui_tars/business.mjs"]
    manifest["files"] = files
    zip_path = _write_zip(tmp_path, manifest, {"App/UI_Tars/Business.MJS": b"x"})
    with pytest.raises(PatchError, match="大小写碰撞"):
        update_patch.load_patch(zip_path)


def test_manifest_rejects_restricted_change(tmp_path):
    manifest = base_manifest(changed=["python/python.exe"])
    zip_path = _write_zip(tmp_path, manifest, {"python/python.exe": b"x"})
    with pytest.raises(PatchScopeError, match="受限区域"):
        update_patch.load_patch(zip_path)


def test_manifest_rejects_missing_content(tmp_path):
    manifest = base_manifest(changed=["app/menu_capture/api/server.py"])
    zip_path = _write_zip(tmp_path, manifest, {})
    with pytest.raises(PatchError, match="缺少修改文件内容"):
        update_patch.load_patch(zip_path)


def test_extract_rejects_duplicate_zip_entries(tmp_path):
    zip_path, _ = build_patch(tmp_path)
    with zipfile.ZipFile(zip_path, "a") as archive:
        archive.writestr("files/app/menu_capture/api/server.py", b"duplicate")
    with pytest.raises(PatchError, match="重复或大小写碰撞"):
        update_patch.apply(
            tmp_path / "base", tmp_path / "stage", zip_path,
            from_version="0.3.1", to_version="0.3.2",
        )


def test_extract_rejects_oversized_member(tmp_path, monkeypatch):
    zip_path, _ = build_patch(tmp_path)
    monkeypatch.setattr(update_patch, "MAX_FILE_BYTES", 4)
    with pytest.raises(PatchError, match="超出限制"):
        update_patch.apply(
            tmp_path / "base", tmp_path / "stage", zip_path,
            from_version="0.3.1", to_version="0.3.2",
        )


def test_scan_tree_rejects_symlink(tmp_path):
    root = write_tree(tmp_path / "base", {"app/a.py": b"a"})
    (root / "app/link.py").symlink_to(root / "app/a.py")
    with pytest.raises(PatchError, match="符号链接"):
        update_patch.scan_tree(root)


def test_scan_tree_accepts_published_bundle_file_count(tmp_path):
    # v0.3.2 和 v0.3.3 的正式 bundle 各有 26,121 个文件。
    root = tmp_path / "release"
    root.mkdir()
    for index in range(26_121):
        (root / f"file-{index:05d}.bin").touch()
    assert len(update_patch.scan_tree(root)) == 26_121


def test_scan_tree_stops_at_release_file_cap(tmp_path, monkeypatch):
    root = write_tree(tmp_path / "release", {"one": b"1", "two": b"2", "three": b"3"})
    monkeypatch.setattr(update_patch, "MAX_RELEASE_FILES", 2)
    with pytest.raises(PatchError, match="文件数量超出"):
        update_patch.scan_tree(root)


@pytest.mark.parametrize("path", [
    "python/Lib/__pycache__/json.cpython-312.pyc",
    "python/Scripts/uvicorn.exe",
    "python/Lib/site-packages/uvicorn-1.dist-info/RECORD",
    "python/python.exe",
    "python/Lib/site-packages/uvicorn/main.py",
    "python/Lib/site-packages/uvicorn-1.dist-info/METADATA",
])
def test_python_runtime_artifacts_remain_restricted(path):
    assert update_patch._is_restricted(path)


def test_make_patch_keeps_patch_operation_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(update_patch, "MAX_PATCH_FILES", 1)
    with pytest.raises(PatchError, match="变更文件数量超出"):
        build_patch(tmp_path)


def test_make_patch_keeps_manifest_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(update_patch, "MAX_MANIFEST_BYTES", 1)
    with pytest.raises(PatchError, match="补丁清单超出大小"):
        build_patch(tmp_path)


def test_apply_ignores_unlisted_base_residue(tmp_path):
    zip_path, _ = build_patch(tmp_path)
    (tmp_path / "base/app/runtime-cache.tmp").write_bytes(b"residue")
    stage = tmp_path / "stage"
    update_patch.apply(
        tmp_path / "base", stage, zip_path, from_version="0.3.1", to_version="0.3.2"
    )
    assert not (stage / "app/runtime-cache.tmp").exists()
    assert update_patch.scan_tree(stage) == update_patch.scan_tree(tmp_path / "target")
