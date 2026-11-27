"""双端前端静态产物放行与体积防回归冒烟测试。

直接封死 PR #135 类“分包资源未放行导致 Hub/Worker 双端白屏”故障：
1. 在 Hub 与 Worker 两种角色下分别访问首页与 manifest 中登记的全部 JS/CSS 分包，
   验证返回 200、非空且 MIME 类型正确；
2. 锁定业务分包体积上限，防止误打入巨型依赖导致首屏加载退化。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from menu_capture.api.server import create_app

_WEB_REACT_DIR = Path(__file__).resolve().parents[2] / "web" / "react"
_MANIFEST_PATH = _WEB_REACT_DIR / ".vite" / "manifest.json"
_INDEX_PATH = _WEB_REACT_DIR / "index.html"


@pytest.mark.parametrize("role", ["hub", "worker"])
def test_frontend_entry_and_all_manifest_chunks_are_served_without_white_screen(tmp_path: Path, role: str) -> None:
    assert _INDEX_PATH.is_file(), "缺少构建后的 web/react/index.html"
    assert _MANIFEST_PATH.is_file(), "缺少构建后的 web/react/.vite/manifest.json"

    kwargs = {"role": role, "admin_password": "secret"} if role == "hub" else {"role": role}
    app = create_app(tmp_path / role, **kwargs)
    auth = ("admin", "secret") if role == "hub" else None

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        index_resp = client.get("/", auth=auth)
        assert index_resp.status_code == 200, f"{role} 首页返回 {index_resp.status_code}"
        html = index_resp.text
        assert '<div id="root">' in html

        # 校验 index.html 中直接引用的静态资源
        direct_assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)
        assert direct_assets, "index.html 未引用任何 /assets/ 构建产物"

        # 校验 manifest.json 中全部懒加载分包均被服务端放行且非空
        manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
        manifest_assets: set[str] = set(direct_assets)
        for entry in manifest.values():
            if file_path := entry.get("file"):
                manifest_assets.add("/" + file_path.lstrip("/"))
            for css in entry.get("css") or []:
                manifest_assets.add("/" + css.lstrip("/"))

        for asset in sorted(manifest_assets):
            resp = client.get(asset, auth=auth)
            assert resp.status_code == 200, f"{role} 角色未放行前端分包 {asset}（HTTP {resp.status_code}）"
            assert len(resp.content) > 0, f"{role} 角色前端分包 {asset} 为空"
            ctype = resp.headers.get("content-type", "")
            if asset.endswith(".js"):
                assert "javascript" in ctype, f"{asset} MIME 异常：{ctype}"
            elif asset.endswith(".css"):
                assert "css" in ctype, f"{asset} MIME 异常：{ctype}"


def test_frontend_bundle_chunk_size_budgets() -> None:
    """前端分包体积预算门禁：防止误引入重型依赖导致首屏退化。"""
    assets_dir = _WEB_REACT_DIR / "assets"
    assert assets_dir.is_dir()
    for js_file in assets_dir.glob("*.js"):
        size_kb = js_file.stat().st_size / 1024
        if js_file.name.startswith("vendor-react"):
            assert size_kb <= 350, f"{js_file.name} 体积 {size_kb:.1f}KB 超出 350KB 预算"
        elif js_file.name.startswith("vendor-"):
            assert size_kb <= 220, f"{js_file.name} 体积 {size_kb:.1f}KB 超出 220KB 预算"
        else:
            assert size_kb <= 120, f"业务分包 {js_file.name} 体积 {size_kb:.1f}KB 超出 120KB 预算"
