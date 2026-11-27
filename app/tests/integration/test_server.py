from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient
from PIL import Image

from menu_capture.api.server import create_app
from menu_capture.projects.service import LEGACY_ID
from menu_capture.storage.store import TaskStore


class FakeDesktop:
    def permissions(self):
        return {"accessibility": True, "screen_recording": True}

    def windows(self):
        return []

    def stop_requested(self):
        return False


def config_payload(root: Path, key: str) -> dict:
    return {
        "input_dir": str(root / "inbox"),
        "output_dir": str(root / "output"),
        "model": {
            "provider": "openai_compatible",
            "base_url": "https://vision.example/v1",
            "model": "vision-test",
            "api_key": key,
        },
    }


def test_restart_recovers_interrupted_task_without_replaying_actions(tmp_path: Path) -> None:
    source = tmp_path / "inbox" / "Cafe" / "qr.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (10, 10), "white").save(source)
    store = TaskStore(tmp_path / "data/tasks.sqlite3")
    store.scan(tmp_path / "inbox")
    task = store.tasks()[0]
    store.update(task["id"], status="running", stage="clicking")
    app = create_app(tmp_path)
    assert app.state.store.get(task["id"])["status"] == "needs_review"
    assert not app.state.runner.running
    assert app.state.runner.run_directory is None


def test_current_material_root_excludes_original_photo_tasks(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    for name in ("inbox", "old-originals"):
        image = tmp_path / name / "Cafe" / "qr.png"
        image.parent.mkdir(parents=True)
        Image.new("RGB", (10, 10), "white").save(image)
        app.state.store.scan(tmp_path / name)
    app.state.configs.save(config_payload(tmp_path, "test-key"))
    client = TestClient(app, base_url="http://127.0.0.1")
    tasks = client.get("/api/status").json()["tasks"]
    assert len(tasks) == 1
    assert "old-originals" not in tasks[0]["source_path"]
    assert len(app.state.store.tasks()) == 2  # 历史仍保留在唯一状态库中。


def test_batch_progress_separates_unqualified_from_needs_review(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    for name in ("Menu", "Unqualified", "Review"):
        source = tmp_path / "inbox" / name / "qr.png"
        source.parent.mkdir(parents=True)
        Image.new("RGB", (10, 10), "white").save(source)
    app.state.configs.save(config_payload(tmp_path, "test-key"))
    store = app.state.store
    store.scan(tmp_path / "inbox")
    ids = {task["source_name"]: task["id"] for task in store.tasks()}
    store.create_run("example-run", "automatic", list(ids.values()))
    store.update(ids["Menu"], status="captured")
    store.update(ids["Unqualified"], status="needs_review", reject_reason="other", error_code="agent_unqualified")
    store.update(ids["Review"], status="needs_review", error_code="agent_needs_review")
    store.update_run("example-run", status="completed", current_index=3)

    client = TestClient(app, base_url="http://127.0.0.1")
    run = client.get("/api/status").json()["run"]
    assert (run["captured"], run["unqualified"], run["review"], run["processed"]) == (1, 1, 1, 3)


def test_config_endpoint_requires_token_and_never_returns_api_key(tmp_path: Path) -> None:
    app = create_app(tmp_path, desktop=FakeDesktop())
    client = TestClient(app, base_url="http://127.0.0.1")
    secret = "do-not-return-this-secret"

    unauthorized = client.post("/api/config", json=config_payload(tmp_path, secret))
    assert unauthorized.status_code == 403
    assert secret not in unauthorized.text

    csrf = client.post(
        "/api/config",
        json=config_payload(tmp_path, secret),
        headers={"X-Capture-Token": app.state.token, "Origin": "http://evil.example"},
    )
    assert csrf.status_code == 403
    assert secret not in csrf.text

    saved = client.post(
        "/api/config",
        json=config_payload(tmp_path, secret),
        headers={"X-Capture-Token": app.state.token, "Origin": "http://127.0.0.1"},
    )
    assert saved.status_code == 200
    assert secret not in saved.text
    assert saved.json()["model"]["api_key_configured"] is True

    public = client.get("/api/config")
    assert public.status_code == 200
    assert secret not in public.text
    assert public.json()["model"]["api_key_configured"] is True


def test_artifact_endpoint_rejects_traversal_outside_task_output(tmp_path: Path) -> None:
    app = create_app(tmp_path, desktop=FakeDesktop())
    client = TestClient(app, base_url="http://127.0.0.1")
    inbox = tmp_path / "inbox"
    source = inbox / "Cafe" / "menu.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (10, 10), "white").save(source)
    task = app.state.store
    task.scan(inbox)
    record = task.tasks()[0]
    output = tmp_path / "output" / record["id"]
    output.mkdir(parents=True)
    task.update(record["id"], output_dir=str(output))
    outside = tmp_path / "outside-secret.json"
    outside.write_text('{"private":"outside"}', encoding="utf-8")

    traversal = quote("../outside-secret.json", safe="")
    response = client.get(f"/api/artifact/{record['id']}/{traversal}")
    assert response.status_code == 404
    assert "outside" not in response.text

    if sys.platform != "win32":
        # Windows 创建软链接需要额外特权，这段只在 POSIX 平台验证。
        link = output / "linked.json"
        link.symlink_to(outside)
        symlink_response = client.get(f"/api/artifact/{record['id']}/linked.json")
        assert symlink_response.status_code == 404
        assert "outside" not in symlink_response.text


def test_react_entry_is_default_and_only_serves_manifest_assets(tmp_path: Path) -> None:
    """根路径始终返回 React 入口，静态资源严格来自 Vite manifest。"""
    import json

    app = create_app(tmp_path, desktop=FakeDesktop())
    client = TestClient(app, base_url="http://127.0.0.1")

    response = client.get("/")
    assert response.status_code == 200
    assert '<div id="root"></div>' in response.text
    assert "/assets/" in response.text
    assert "__CAPTURE_TOKEN__" not in response.text
    assert response.headers["cache-control"] == "no-store"
    # 组件 CSS 可能由 Vite 先输出；在所有样式表之前确定层级，防止基础重置覆盖组件。
    assert response.text.index("@layer properties, theme, base, components, utilities;") < response.text.index(
        'rel="stylesheet"'
    )

    # 迁移期临时入口不再存在：旧参数与旧版横幅都会被忽略。
    assert client.get("/", params={"ui": "vue"}).text == response.text

    manifest = json.loads(
        (Path(__file__).resolve().parents[2] / "web/react/.vite/manifest.json").read_text(encoding="utf-8")
    )
    entry = next(item for item in manifest.values() if item.get("isEntry"))
    script = entry["file"].rsplit("/", 1)[-1]
    asset = client.get(f"/assets/{script}")
    assert asset.status_code == 200
    assert "javascript" in asset.headers["content-type"]
    assert "immutable" in asset.headers["cache-control"]

    # 分包后 HTML 的预加载资源和业务页懒加载资源也必须能从后端直接读取。
    for key in entry.get("imports", []) + entry.get("dynamicImports", []):
        chunk = manifest[key]
        for path in [chunk["file"], *chunk.get("css", [])]:
            response = client.get(f"/{path}")
            assert response.status_code == 200, path
            assert "immutable" in response.headers["cache-control"]

    version = client.get("/api/version").json()
    assert version["frontend"] == script
    assert client.get("/favicon.svg").status_code == 200

    # 目录内的 index.html、清单外文件、旧版资源与越界路径都不能通过资源路由读取。
    assert client.get("/assets/index.html").status_code == 404
    assert client.get("/assets/not-exists.js").status_code == 404
    assert client.get("/assets/materials.js").status_code == 404
    assert client.get("/assets/%2e%2e/index.html").status_code != 200
    assert client.get("/vue/task-center.js").status_code == 404


def test_react_entry_reports_missing_build(tmp_path: Path, monkeypatch) -> None:
    """未构建前端时明确返回 503，而不是回退到任何旧版页面。"""
    from menu_capture.api import server

    monkeypatch.setattr(server, "ROOT", tmp_path)
    app = create_app(tmp_path / "data", desktop=FakeDesktop())
    client = TestClient(app, base_url="http://127.0.0.1")

    response = client.get("/")
    assert response.status_code == 503
    assert "前端" in response.text


def test_materials_history_detail_counts_match_list(tmp_path: Path) -> None:
    """素材任务详情必须返回与列表相同的进度口径，不能返回空 counts。"""
    app = create_app(tmp_path)
    store = app.state.store
    stamp = "2026-11-23T10:00:00+08:00"
    with store.connect() as db:
        db.execute(
            "INSERT INTO material_batches(id,source_kind,source_root,status,created_at,updated_at,name,project_id) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("batch-a", "folder", str(tmp_path / "src"), "released", stamp, stamp, "演示批次", LEGACY_ID),
        )
        db.execute(
            "INSERT INTO material_jobs(id,batch_id,kind,status,total_stores,done_stores,failed_stores,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("job-a", "batch-a", "analyze", "running", 32, 18, 1, stamp, stamp),
        )
    client = TestClient(app, base_url="http://127.0.0.1")

    detail = client.get("/api/tasks/history/materials/job-a").json()
    assert detail["counts"] == {"total": 32, "done": 18, "attention": 0, "failed": 1}
    assert detail["batch_name"] == "演示批次"
    listed = client.get("/api/tasks/history", params={"kind": "materials"}).json()["records"]
    assert listed[0]["counts"] == detail["counts"]


def test_history_detail_and_task_route_expose_observed_store_name(tmp_path: Path) -> None:
    """伴随提取的门店名称：逐店明细带出，且可按原始任务编号直接查询（含未采集成功）。"""
    app = create_app(tmp_path)
    source = tmp_path / "inbox" / "Menu" / "qr.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (10, 10), "white").save(source)
    app.state.configs.save(config_payload(tmp_path, "test-key"))
    store = app.state.store
    store.scan(tmp_path / "inbox")
    task = store.tasks()[0]
    store.create_run("run-name", "automatic", [task["id"]])
    store.update(task["id"], status="needs_review", error_code="agent_needs_review")
    store.update_run("run-name", status="completed", current_index=1)
    store.record_observed_store_name("run-name", task["id"], "老王饭店（二店）",
                                     frame_id="4", frame="ui-tars-004.png")
    client = TestClient(app, base_url="http://127.0.0.1")

    detail = client.get("/api/tasks/history/capture/run-name").json()
    assert detail["tasks"][0]["observed_store_name"] == "老王饭店（二店）"
    record = client.get(f"/api/tasks/observed-store-name/{task['id']}").json()
    assert record["store_name"] == "老王饭店（二店）"
    assert record["run_id"] == "run-name" and record["frame"] == "ui-tars-004.png"
    assert client.get("/api/tasks/observed-store-name/missing").status_code == 404


def test_remote_manual_control_is_scoped_and_rejects_stale_commands(tmp_path):
    from base64 import b64encode

    password = "isolated-test-password"
    origin = "https://capture.test"
    auth = {"Authorization": "Basic " + b64encode(f"operator:{password}".encode()).decode()}
    app = create_app(tmp_path, role="worker", remote_control_host="capture.test",
                     remote_control_password=password)
    client = TestClient(app, base_url=origin)
    local = TestClient(app, base_url="http://127.0.0.1")
    runner = app.state.runner
    runner.run_id = "run-1"
    task = {"id": "task-a", "source_name": "测试门店"}
    runner._set_manual_state(task, 0, 1, "waiting_human", waiting=True)

    assert client.get("/remote-control").status_code == 401
    assert client.get("/remote-control", headers=auth).status_code == 200
    assert client.get("/api/config", headers=auth).status_code == 403
    assert client.get("/", headers=auth).status_code == 403
    assert TestClient(app, base_url="http://capture.test").get("/remote-control", headers=auth).status_code == 403

    state = client.get("/api/manual/state", headers=auth).json()
    context = {key: state["manual"][key] for key in ("run_id", "task_id", "wait_id")}
    context["device_id"] = state["device_id"]
    command = {"context": context}
    token = client.get("/api/session", headers=auth).json()["token"]
    assert token != local.get("/api/session").json()["token"]
    headers = auth | {"x-capture-token": token, "Origin": origin}
    assert client.post("/api/manual/capture", headers=auth | {"x-capture-token": token}, json=command).status_code == 403
    assert client.post("/api/manual/capture", headers=headers, json={}).status_code == 409
    assert client.post("/api/manual/capture", headers=headers,
                       json={"context": context | {"device_id": "other"}}).status_code == 409

    # 同一家店重新进入等待时，以新等待标识校验，旧标签页不能触发动作。
    runner._set_manual_state(task, 0, 1, "waiting_human", waiting=True)
    assert client.post("/api/manual/capture", headers=headers, json=command).status_code == 409
    command["context"]["wait_id"] = runner.manual_state()["wait_id"]
    accepted = client.post("/api/manual/capture", headers=headers, json=command)
    assert accepted.status_code == 200 and accepted.json()["task_id"] == "task-a"
    assert "status" not in accepted.json()
    assert client.post("/api/manual/capture", headers=headers, json=command).status_code == 409
    app.state.shared_worker.close()


def test_recovery_shutdown_is_loopback_scoped_and_requires_coordinator_token(
    tmp_path: Path, monkeypatch,
) -> None:
    from types import SimpleNamespace

    coordinator_token = "test-coordinator-token"
    monkeypatch.setenv("MENU_CAPTURE_COORDINATOR_TOKEN", coordinator_token)
    app = create_app(tmp_path, role="worker")
    install = app.state.updates_install
    authorized: list[tuple[str, str]] = []

    def authorize(transaction_id: str, target_version: str) -> None:
        if transaction_id != "transaction-1" or target_version != "0.4.0":
            raise ValueError("恢复关闭请求不对应当前验证事务")
        authorized.append((transaction_id, target_version))

    install.authorize_recovery_shutdown = authorize
    app.state.uvicorn_server = SimpleNamespace(should_exit=False)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345))
    payload = {"transaction_id": "stale-transaction", "target_version": "0.4.0"}

    denied = client.post("/api/internal/recovery-shutdown", json=payload)
    assert denied.status_code == 401
    stale = client.post("/api/internal/recovery-shutdown", json=payload,
                        headers={"X-Coordinator-Token": coordinator_token})
    assert stale.status_code == 409
    assert not authorized

    # 一个非回环 Host 即使携带有效协调令牌，也不允许调用内部关闭协议。
    remote = TestClient(app, base_url="http://capture.example", client=("127.0.0.1", 12345))
    assert remote.post("/api/internal/recovery-shutdown", json=payload,
                       headers={"X-Coordinator-Token": coordinator_token}).status_code == 403

    payload["transaction_id"] = "transaction-1"
    accepted = client.post("/api/internal/recovery-shutdown", json=payload,
                           headers={"X-Coordinator-Token": coordinator_token})
    assert accepted.status_code == 200 and accepted.json() == {"ok": True}
    assert authorized == [("transaction-1", "0.4.0")]
    assert app.state.uvicorn_server.should_exit is True


def test_update_install_is_disabled_without_explicit_opt_in(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {"x-capture-token": client.get("/api/session").json()["token"]}

    status = client.get("/api/updates/install")
    assert status.status_code == 200
    assert status.json()["supported"] is False

    response = client.post("/api/updates/install", headers=headers,
                           json={"version": "0.3.0", "confirm": "0.3.0"})
    assert response.status_code == 409
    assert "未启用自动安装" in response.json()["detail"]


def test_update_download_endpoint_requires_checked_release(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {"x-capture-token": client.get("/api/session").json()["token"]}

    response = client.post("/api/updates/download", headers=headers, json={})
    assert response.status_code == 409
    assert "没有可下载的更新" in response.json()["detail"]

    cancelled = client.post("/api/updates/download/cancel", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "idle"
