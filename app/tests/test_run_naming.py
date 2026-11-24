"""采集任务改名与名称回退口径：只改展示名，进度、批次和历史结果保持原字段。"""
from pathlib import Path
import threading

from fastapi.testclient import TestClient

from menu_capture.server import create_app


def _client(app):
    return TestClient(app, base_url="http://127.0.0.1")


def test_rename_keeps_progress_and_is_searchable(tmp_path: Path):
    app = create_app(tmp_path)
    store = app.state.store
    store.create_run("run-a", "automatic", ["t1", "t2", "t3"], capture_mode="auto")
    store.update_run("run-a", current_index=2, status="completed")
    created = store.get_run("run-a")
    with _client(app) as client:
        headers = {"X-Capture-Token": app.state.token}
        response = client.patch("/api/tasks/history/capture/run-a",
                                json={"name": "  第三批返工  "}, headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["title"] == "第三批返工"
        assert body["counts"]["total"] == 3

        detail = client.get("/api/tasks/history/capture/run-a").json()
        assert detail["title"] == "第三批返工"
        listed = client.get("/api/tasks/history", params={"kind": "capture"}).json()["records"]
        assert [row["title"] for row in listed] == ["第三批返工"]
        found = client.get("/api/tasks/history", params={"q": "第三批"}).json()
        assert [row["id"] for row in found["records"]] == ["run-a"]

    saved = store.get_run("run-a")
    # 改名不触碰执行进度、状态、创建时间和批次归属。
    assert (saved["current_index"], saved["status"]) == (2, "completed")
    for key in ("created_at", "updated_at", "task_ids", "capture_mode", "material_batch_id"):
        assert saved[key] == created[key]


def test_rename_rejects_blank_overlong_and_missing_run(tmp_path: Path):
    app = create_app(tmp_path)
    app.state.store.create_run("run-b", "automatic", ["t1"], capture_mode="auto")
    with _client(app) as client:
        headers = {"X-Capture-Token": app.state.token}

        def rename(run_id, payload):
            return client.patch(f"/api/tasks/history/capture/{run_id}", json=payload, headers=headers)

        assert rename("run-b", {"name": "   "}).status_code == 400
        assert rename("run-b", {"name": "名" * 65}).status_code == 400
        assert rename("run-b", {"name": None}).status_code == 400
        assert rename("run-b", {}).status_code == 400
        missing = rename("run-missing", {"name": "有效名称"})
        assert missing.status_code == 404
        assert missing.json()["detail"] == "运行记录不存在"
        # 校验失败不写入任何名称，原回退标题保持不变。
        assert app.state.store.get_run("run-b")["name"] == ""


def test_rename_and_progress_write_concurrently(tmp_path: Path):
    app = create_app(tmp_path)
    store = app.state.store
    store.create_run("run-c", "automatic", ["t1", "t2"], capture_mode="auto")
    barrier = threading.Barrier(2)

    def rename():
        barrier.wait()
        for index in range(20):
            store.rename_run("run-c", f"并发改名 {index}")

    def progress():
        barrier.wait()
        for index in range(20):
            store.update_run("run-c", current_index=index, status="running")

    threads = [threading.Thread(target=rename), threading.Thread(target=progress)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    saved = store.get_run("run-c")
    assert saved["name"] == "并发改名 19"
    assert saved["current_index"] == 19


def test_title_fallback_uses_mode_and_count(tmp_path: Path):
    app = create_app(tmp_path)
    store = app.state.store
    store.create_run("old", "automatic", ["a", "b"])
    store.create_run("auto", "automatic", ["a", "b"], capture_mode="auto")
    store.create_run("placeholder", "automatic", ["a"], capture_mode="auto", name="automatic")
    store.create_run("saved", "automatic", ["a"], capture_mode="manual", name="人工补采")
    with _client(app) as client:
        rows = {row["id"]: row["title"] for row in
                client.get("/api/tasks/history", params={"kind": "capture"}).json()["records"]}
    assert rows["old"] == "批量采集 · 2 家"
    assert rows["auto"] == "全自动采集 · 2 家"
    assert rows["placeholder"] == "全自动采集 · 1 家"
    assert rows["saved"] == "人工补采"
