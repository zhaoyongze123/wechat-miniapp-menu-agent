from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "deploy" / "windows" / "prepare_python_bundle.py"
SPEC = importlib.util.spec_from_file_location("prepare_python_bundle", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)


def _record_hash(content: bytes) -> str:
    digest = hashlib.sha256(content).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _python_tree(tmp_path: Path, *, launcher_hash: str | None = None) -> tuple[Path, Path, Path]:
    root = tmp_path / "python"
    scripts = root / "Scripts"
    scripts.mkdir(parents=True)
    launcher = scripts / "uvicorn.exe"
    launcher.write_bytes(b"launcher")
    bytecode = root / "Lib" / "__pycache__" / "module.cpython-312.pyc"
    bytecode.parent.mkdir(parents=True)
    bytecode.write_bytes(b"cache")
    (root / "Lib" / "module.py").write_text("value = 1\n", encoding="utf-8")
    record = root / "Lib" / "site-packages" / "example-1.0.dist-info" / "RECORD"
    record.parent.mkdir(parents=True)
    rows = [
        ["example.py", "sha256=preserve", "5"],
        ["../../Scripts/uvicorn.exe", launcher_hash or _record_hash(b"launcher"), "8"],
        ["../../Scripts/pip.exe", "", ""],
        ["example-1.0.dist-info/RECORD", "", ""],
    ]
    with record.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows)
    return root, launcher, bytecode


def test_prepare_removes_build_bytecode_and_unused_launchers_updates_record(tmp_path):
    root, launcher, bytecode = _python_tree(tmp_path)
    protected = root / "python.exe"
    protected.write_bytes(b"runtime")
    app_root = tmp_path / "app"
    app_bytecode = app_root / "menu_capture" / "__pycache__" / "server.cpython-312.pyc"
    app_bytecode.parent.mkdir(parents=True)
    app_bytecode.write_bytes(b"compiled app cache")
    (app_root / "menu_capture" / "server.py").write_text("value = 1\n", encoding="utf-8")
    orphan = root / "Lib" / "site-packages" / "binary_only" / "__pycache__" / "module.cpython-312.pyc"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"pyc-only executable module")

    result = PREPARE.prepare_python_bundle(root, app_root=app_root)

    assert result == {
        "python_bytecode_files_removed": 1,
        "app_bytecode_files_removed": 1,
        "script_launchers_removed": 1,
    }
    assert not launcher.exists()
    assert not bytecode.exists()
    assert not app_bytecode.exists()
    assert orphan.exists(), "source-less bytecode must remain available to the runtime"
    assert protected.read_bytes() == b"runtime"
    assert not (root / "Lib" / "__pycache__").exists()
    assert not (app_root / "menu_capture" / "__pycache__").exists()
    with (root / "Lib/site-packages/example-1.0.dist-info/RECORD").open(encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows == [
        ["example.py", "sha256=preserve", "5"],
        ["example-1.0.dist-info/RECORD", "", ""],
    ]


def test_prepare_rejects_launcher_record_mismatch_before_mutation(tmp_path):
    root, launcher, bytecode = _python_tree(tmp_path, launcher_hash="sha256=wrong")
    record = root / "Lib/site-packages/example-1.0.dist-info/RECORD"
    original_record = record.read_bytes()

    with pytest.raises(ValueError, match="RECORD mismatch"):
        PREPARE.prepare_python_bundle(root)

    assert launcher.read_bytes() == b"launcher"
    assert bytecode.read_bytes() == b"cache"
    assert record.read_bytes() == original_record


def test_prepare_rejects_unrecorded_launcher_before_mutation(tmp_path):
    root, launcher, bytecode = _python_tree(tmp_path)
    record = root / "Lib/site-packages/example-1.0.dist-info/RECORD"
    with record.open(encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    rows = [row for row in rows if not row[0].endswith("uvicorn.exe")]
    with record.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows)
    original_record = record.read_bytes()

    with pytest.raises(ValueError, match="missing from RECORD"):
        PREPARE.prepare_python_bundle(root)

    assert launcher.exists()
    assert bytecode.exists()
    assert record.read_bytes() == original_record
