from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from menu_capture.window_contract import validate_target_window


PROJECT_ROOT = Path(__file__).resolve().parents[3] / "UFS_Menu_Capture_CrossPlatform"
MAIN_XAML = PROJECT_ROOT / "Main.xaml"

NS = {
    "wf": "http://schemas.microsoft.com/netfx/2009/xaml/activities",
    "ui": "http://schemas.uipath.com/workflow/activities",
    "uix": "http://schemas.uipath.com/workflow/activities/uix",
    "x": "http://schemas.microsoft.com/winfx/2006/xaml",
}


def _root() -> ET.Element:
    return ET.parse(MAIN_XAML).getroot()


def _invoke_code(root: ET.Element, id_ref: str) -> ET.Element:
    for node in root.findall(".//ui:InvokeCode", NS):
        if node.attrib.get("{http://schemas.microsoft.com/netfx/2010/xaml/activities/presentation}WorkflowViewState.IdRef") == id_ref:
            return node
    raise AssertionError(f"InvokeCode {id_ref} is missing")


def test_window_attributes_are_measured_inside_the_same_uipath_flow() -> None:
    root = _root()
    attribute_reads = [
        node for node in root.findall(".//uix:NGetAttributeGeneric", NS)
        if node.attrib.get("Attribute") in {"app", "title", "foreground"}
    ]

    assert {node.attrib["Attribute"] for node in attribute_reads} == {"app", "title", "foreground"}
    assert all(node.attrib.get("ContinueOnError") != "True" for node in attribute_reads)
    assert all(node.find("uix:NGetAttributeGeneric.Target/uix:TargetAnchorable", NS) is not None for node in attribute_reads)
    assert all(node.find("uix:NGetAttributeGeneric.Target/uix:TargetAnchorable", NS).attrib.get("FullSelectorArgument") == "[selector]" for node in attribute_reads)

    activation = next(node for node in root.findall(".//uix:NWindowOperation", NS) if node.attrib.get("Operation") == "ToForeground")
    keyboard = root.find(".//uix:NKeyboardShortcuts", NS)
    assert keyboard is not None
    ordered = list(root.iter())
    assert ordered.index(activation) < min(ordered.index(node) for node in attribute_reads)
    assert max(ordered.index(node) for node in attribute_reads) < ordered.index(keyboard)

    boundary = next(
        node for node in root.findall(".//wf:If", NS)
        if 'action = "screenshot"' in node.attrib.get("Condition", "")
        and 'action = "key"' in node.attrib.get("Condition", "")
        and 'action = "type_text"' in node.attrib.get("Condition", "")
    )
    assert "scroll_capture" not in boundary.attrib["Condition"]


def test_keyboard_requires_measured_foreground_and_activates_target() -> None:
    root = _root()
    keyboard = root.find(".//uix:NKeyboardShortcuts", NS)
    assert keyboard is not None
    assert keyboard.attrib.get("ActivateBefore") == "True"

    guards = [node.attrib.get("Condition", "") for node in root.findall(".//wf:If", NS)]
    assert any('(action = "key" OrElse action = "type_text" OrElse action = "screenshot") AndAlso Not targetForeground' in condition for condition in guards)
    assert any("foreground=false" in (node.attrib.get("Exception", "")) for node in root.findall(".//wf:Throw", NS))

    retry = next(node for node in root.findall(".//wf:If", NS) if node.attrib.get("{http://schemas.microsoft.com/netfx/2010/xaml/activities/presentation}WorkflowViewState.IdRef") == "If_RetryForegroundOnce")
    assert '(action = "key" OrElse action = "type_text") AndAlso Not targetForeground' in retry.attrib["Condition"]
    retry_block = retry.find("wf:If.Then/wf:Sequence", NS)
    assert retry_block is not None
    assert len(retry_block.findall("uix:NWindowOperation", NS)) == 1
    assert retry_block.find("wf:Delay", NS).attrib["Duration"] == "00:00:00.2500000"
    assert {node.attrib.get("Attribute") for node in retry_block.findall("uix:NGetAttributeGeneric", NS)} == {"app", "title", "foreground"}
    assert not retry_block.findall(".//uix:NKeyboardShortcuts", NS)
    assert not retry_block.findall(".//uix:NClick", NS)


def test_clicks_activate_except_desktop_overlay_click() -> None:
    root = _root()
    clicks = root.findall(".//uix:NClick", NS)
    assert len(clicks) == 3
    assert clicks[0].attrib.get("ActivateBefore") == '[action <> "click_desktop_overlay"]'
    assert all(node.attrib.get("ActivateBefore") == "True" for node in clicks[1:])


def test_reply_reports_measured_target_and_desktop_scope_without_window_claim() -> None:
    root = _root()
    reply = _invoke_code(root, "InvokeCode_3").attrib["Code"]
    assert 'payload("target_window")' in reply
    assert '{"selector", selector}' in reply
    assert '{"app", appValue}' in reply
    assert '{"title", titleValue}' in reply
    assert '{"foreground", foregroundValue}' in reply
    assert '{"capture_scope", captureScope}' in reply
    assert 'If(action = "screenshot_desktop", "desktop", "window")' in reply
    assert 'If(targetWindowMeasured, CType(targetForeground, Object), Nothing)' in reply

    desktop = next(node for node in root.findall(".//uix:NTakeScreenshot", NS) if node.attrib.get("IsDesktop") == "True")
    assert desktop.attrib.get("DisplayName") == "截取整个桌面"


def test_per_command_window_metadata_is_reset_before_processing() -> None:
    root = _root()
    reset_ids = {"Assign_ResetTargetApp", "Assign_ResetTargetTitle", "Assign_ResetTargetForeground", "Assign_ResetTargetWindowMeasured"}
    found = {
        node.attrib.get("{http://schemas.microsoft.com/netfx/2010/xaml/activities/presentation}WorkflowViewState.IdRef")
        for node in root.findall(".//wf:Assign", NS)
    }
    assert reset_ids <= found

    app_reset = next(node for node in root.findall(".//wf:Assign", NS) if node.attrib.get("{http://schemas.microsoft.com/netfx/2010/xaml/activities/presentation}WorkflowViewState.IdRef") == "Assign_ResetTargetApp")
    title_reset = next(node for node in root.findall(".//wf:Assign", NS) if node.attrib.get("{http://schemas.microsoft.com/netfx/2010/xaml/activities/presentation}WorkflowViewState.IdRef") == "Assign_ResetTargetTitle")
    assert app_reset.find("wf:Assign.Value/wf:InArgument", NS).text == "[Nothing]"
    assert title_reset.find("wf:Assign.Value/wf:InArgument", NS).text == "[Nothing]"


def _selector(app: str = "WeChat", title: str = "微信") -> str:
    return f"<wnd app='{app}' role='AXWindow' title='{title}' />"


def _command(action: str = "screenshot") -> dict:
    return {"action": action, "selector": _selector(), "path": "/tmp/frame.png"}


def _target_window(**changes) -> dict:
    value = {
        "selector": _selector(), "app": "WeChat", "title": "微信",
        "foreground": True, "capture_scope": "window",
    }
    value.update(changes)
    return value


def test_window_frame_rejects_another_app_even_if_command_succeeded() -> None:
    with pytest.raises(ValueError, match="窗口 app 与请求不一致"):
        validate_target_window(
            _command(),
            {"id": "1", "ok": True, "target_window": _target_window(app="Google Chrome")},
        )


def test_window_frame_rejects_occluded_or_stale_window() -> None:
    with pytest.raises(ValueError, match="未置前"):
        validate_target_window(
            _command("key"),
            {"id": "1", "ok": True, "target_window": _target_window(foreground=False)},
        )


def test_window_frame_requires_measured_identity_not_selector_claim() -> None:
    with pytest.raises(ValueError, match="未返回实际窗口身份"):
        validate_target_window(_command(), {"id": "1", "ok": True})


def test_desktop_frame_does_not_claim_to_be_the_wechat_window() -> None:
    validate_target_window(
        _command("screenshot_desktop"),
        {"id": "1", "ok": True, "target_window": {
            "selector": _selector(), "app": None, "title": None,
            "foreground": None, "capture_scope": "desktop",
        }},
    )

    with pytest.raises(ValueError, match="观察范围与请求不一致"):
        validate_target_window(
            _command(),
            {"id": "1", "ok": True, "target_window": _target_window(capture_scope="desktop")},
        )

