from __future__ import annotations

import json
from pathlib import Path

from menu_capture.ui_tars_trial import _HOST_COMMANDS

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "ui_tars" / "action-schema.json"


def _schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_host_commands_cover_schema():
    """Node 侧按 host.method 发命令，Python 必须有同名处理函数，否则调用会落到“试验范围以外”。"""
    actions = _schema()["actions"]
    expected = {
        spec["host"]["method"]
        for name, spec in actions.items()
        if name != "terminate" and spec.get("host")
    }
    assert expected == set(_HOST_COMMANDS)


def test_schema_mapping_only_references_declared_fields():
    """host.map 里的字段必须来自 required/optional，避免映射到一个模型永远不会提供的字段。"""
    for name, spec in _schema()["actions"].items():
        host = spec.get("host")
        if not host:
            continue
        declared = {"action", *spec.get("required", {}), *spec.get("optional", {})}
        for param, expression in host.get("map", {}).items():
            if not isinstance(expression, str) or expression.startswith("$"):
                continue
            field = expression.split("[", 1)[0]
            assert field in declared, f"{name}.{param} -> {expression}"


def test_terminal_action_has_no_host_mapping():
    spec = _schema()["actions"]["terminate"]
    assert spec["host"] is None
    assert spec["required"]["outcome"]["values"] == ["success", "unqualified", "needs_review"]
