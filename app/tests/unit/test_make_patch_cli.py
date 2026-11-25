from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "deploy" / "windows" / "make_patch.py"
SPEC = importlib.util.spec_from_file_location("make_patch_cli", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MAKE_PATCH_CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MAKE_PATCH_CLI)


def test_emit_is_safe_for_windows_legacy_console_encoding():
    payload = {"status": "skipped", "reason": "文件数量超出补丁允许范围"}
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")

    MAKE_PATCH_CLI._emit(payload, stream=stream)
    stream.flush()

    assert b"\\u6587" in raw.getvalue()
    assert json.loads(raw.getvalue().decode("ascii")) == payload
