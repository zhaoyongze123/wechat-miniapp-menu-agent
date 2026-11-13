"""Windows QR 场景的多窗口坐标回归测试。"""

from pathlib import Path
import xml.etree.ElementTree as ET

from PIL import Image

from menu_capture.uipath_runner import FileRobotTransport


class _LocalWindowDriver:
    actions = frozenset({"screenshot", "click"})

    def __init__(self, geometry):
        self.geometry = geometry
        self.commands = []

    def execute(self, command):
        self.commands.append(dict(command))
        selector = command["selector"]
        width, height = self.geometry[selector]
        reply = {
            "ok": True,
            "action": command["action"],
            "target_window": {
                "app": "Weixin.exe",
                "title": ET.fromstring(selector).attrib["title"],
                "class": "QtWindow",
                "foreground": True,
                "capture_scope": "window",
            },
            "window_x": 10 if selector.endswith("preview") else 500,
            "window_y": 20,
            "window_width": width,
            "window_height": height,
            "window_id": f"123:{width}",
        }
        if command["action"] == "screenshot":
            path = Path(command["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (width * 2, height * 2), "white").save(path)
            reply.update(path=str(path.resolve()), width=width * 2, height=height * 2)
        return reply


def test_qr_click_uses_geometry_of_its_selector_after_other_window_screenshot(tmp_path):
    """截图预览窗后，点击小程序仍须使用小程序自身的截图边界。"""
    miniapp = "<wnd app='Weixin.exe' title='小程序' />"
    preview = "<wnd app='Weixin.exe' title='二维码预览' />"
    driver = _LocalWindowDriver({miniapp: (480, 720), preview: (240, 360)})
    transport = FileRobotTransport(tmp_path / "ipc", tmp_path / "out", local=driver)

    transport.run({"action": "screenshot", "selector": miniapp,
                   "path": str(tmp_path / "out" / "miniapp.png")})
    transport.run({"action": "screenshot", "selector": preview,
                   "path": str(tmp_path / "out" / "preview.png")})
    transport.run({"action": "click", "selector": miniapp, "x": 430, "y": 650})

    click = driver.commands[-1]
    assert click["selector"] == miniapp
    assert (click["expected_window_width"], click["expected_window_height"]) == (480, 720)
    assert click["expected_window_id"] == "123:480"
    assert (click["x"], click["y"]) == (430, 650)


def test_preview_must_show_current_photo(tmp_path):
    from PIL import ImageDraw

    from menu_capture.chat_image_locator import match_preview_source

    current = Image.new("RGB", (240, 270), (225, 153, 30))
    draw = ImageDraw.Draw(current)
    draw.rectangle((24, 36, 156, 192), fill=(15, 21, 53))
    draw.ellipse((91, 105, 206, 239), fill=(237, 233, 184))
    previous = Image.new("RGB", (240, 270), (0, 132, 30))
    ImageDraw.Draw(previous).rectangle((5, 5, 60, 130), fill=(230, 230, 230))
    source_path = tmp_path / "original.png"
    preview_path = tmp_path / "preview.png"
    current.save(source_path)
    preview = Image.new("RGB", (540, 315), "white")
    preview.save(preview_path)
    assert not match_preview_source(preview_path, source_path)["matched"]

    preview.paste(previous, (150, 38))
    preview.save(preview_path)
    assert not match_preview_source(preview_path, source_path)["matched"]

    preview.paste(current, (150, 38))
    preview.save(preview_path)
    assert match_preview_source(preview_path, source_path)["matched"]


def test_window_identity_stays_stable_through_title_and_size_change():
    from menu_capture.uipath_runner import UiPathRunner

    prior = {"window_id": "42:100", "title": "", "position": {"width": 100}}
    same = {"window_id": "42:100", "title": "微信", "position": {"width": 450}}
    new = {**same, "window_id": "42:101"}
    assert UiPathRunner._new_windows([prior], [same, new]) == [new]
