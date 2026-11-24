"""React 静态入口、资源白名单与版本协议。"""
from __future__ import annotations

from fastapi import FastAPI
import json
import os
from pathlib import Path, PurePosixPath
from fastapi import HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from menu_capture.core.config import ROOT


def register_frontend_routes(
    app: FastAPI,
    *,
    react_root,
    worker,
) -> None:
    def react_bundle() -> tuple[str, dict[str, Path], str]:
        """读取一次 manifest，返回入口 HTML、允许对外提供的资源清单与入口文件名。"""
        try:
            manifest = json.loads((react_root / ".vite" / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError("前端控制台尚未构建，请先运行 web-react 的 npm run build") from error
        entries = [item for item in manifest.values()
                   if isinstance(item, dict) and item.get("isEntry")] if isinstance(manifest, dict) else []
        if len(entries) != 1 or not isinstance(entries[0].get("file"), str):
            raise RuntimeError("前端构建清单缺少唯一入口文件")
        entry = entries[0]
        assets: dict[str, Path] = {}
        names = [entry["file"], *[item for item in entry.get("css", []) if isinstance(item, str)]]
        for name in names:
            relative = PurePosixPath(name)
            # 只接受 assets/<文件名>，拒绝绝对路径、..、嵌套目录与隐藏文件。
            if (relative.is_absolute() or len(relative.parts) != 2 or relative.parts[0] != "assets"
                    or relative.parts[1].startswith(".")):
                raise RuntimeError("前端构建清单包含非法资源路径")
            target = react_root.joinpath(*relative.parts)
            if not target.is_file():
                raise RuntimeError(f"前端构建产物缺失: {name}")
            assets[relative.parts[1]] = target
        try:
            html = (react_root / "index.html").read_text(encoding="utf-8")
        except OSError as error:
            raise RuntimeError("前端入口 HTML 缺失") from error
        return html, assets, entry["file"]

    @app.get("/", response_class=HTMLResponse)
    def index():
        try:
            html, _, _ = react_bundle()
        except RuntimeError as error:
            return HTMLResponse(str(error), status_code=503)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/remote-control", response_class=HTMLResponse)
    def remote_control_page():
        if worker is None:
            raise HTTPException(404, "半自动控制仅在 Windows 采集端可用")
        return index()

    @app.get("/assets/{name}")
    def react_assets(name: str):
        """前端静态资源：按 manifest 白名单返回内容哈希文件，长期缓存。"""
        try:
            _, assets, _ = react_bundle()
        except RuntimeError as error:
            raise HTTPException(503, str(error)) from error
        target = assets.get(name)
        media_types = {
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".woff2": "font/woff2",
            ".png": "image/png",
        }
        media_type = media_types.get(target.suffix.lower()) if target else None
        if target is None or media_type is None:
            raise HTTPException(404, "资源不存在")
        return FileResponse(target, media_type=media_type,
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/favicon.svg")
    def favicon():
        target = react_root / "favicon.svg"
        if not target.is_file():
            raise HTTPException(404, "资源不存在")
        return FileResponse(target, media_type="image/svg+xml",
                            headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/version")
    def version_info():
        # 发布版本由安装启动器或镜像环境的 MENU_CAPTURE_VERSION 提供；
        # 入口哈希文件名即前端构建 ID，便于升级/回滚后核对实际运行产物。
        frontend = ""
        try:
            _, _, entry = react_bundle()
            frontend = PurePosixPath(entry).name
        except RuntimeError:
            frontend = ""
        version = os.environ.get("MENU_CAPTURE_VERSION", "")
        commit = ""
        # 老守护进程升级时仍在内存中执行旧启动脚本，可能没有传入版本环境变量。
        # 从本次运行目录的发布清单补齐，绝不根据其他版本目录推断；提交号用于确认运行的就是已安装构建。
        try:
            release = json.loads((ROOT.parent / "release-manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, AttributeError):
            release = {}
        if isinstance(release, dict):
            if worker:
                version = str(release.get("version") or version)
            commit = str(release.get("git_commit") or "")
        return {"version": version, "frontend": frontend, "commit": commit}
