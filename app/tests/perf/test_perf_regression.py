"""长期性能回归门禁（Performance Regression Testing）。

设计原则（0% CI 误报）：
1. 确定性结构门禁：使用 SQLite `set_trace_callback` 精确统计核心读/写接口的 SQL 执行次数，
   直接锁死 N+1 查询回归；同时校验关键大表查询的 `EXPLAIN QUERY PLAN` 命中索引。
2. I/O 与计算次数门禁：验证 100 家门店批量发布不重复哈希同一原图、增量发布不对未改动门店
   重复做 PIL 像素解码、跨批次批量打回只扫描一次有效任务。
3. 不依赖绝对毫秒数做脆弱断言（避免不同 CI Runner 抖动），只对明显退化设置合理量级上限。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from menu_capture.api.server import create_app
from menu_capture.core.config import ConfigStore
from menu_capture.materials import materials as materials_mod
from menu_capture.materials import store_material_versions as smv_mod
from menu_capture.materials.materials import MaterialService
from menu_capture.storage.store import TaskStore, now


@contextlib.contextmanager
def count_sql(store: TaskStore):
    """统计代码块内通过 TaskStore.connect() 执行的 DML/SELECT 语句条数。"""
    counts = {"total": 0, "queries": []}
    original_connect = store.connect

    @contextlib.contextmanager
    def traced_connect():
        with original_connect() as conn:
            def _trace(stmt: str) -> None:
                upper = stmt.strip().upper()
                if upper.startswith(("SELECT", "INSERT", "UPDATE", "DELETE")):
                    counts["total"] += 1
                    counts["queries"].append(" ".join(stmt.strip().split())[:100])

            conn.set_trace_callback(_trace)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    store.connect = traced_connect  # type: ignore[method-assign]
    try:
        yield counts
    finally:
        store.connect = original_connect  # type: ignore[method-assign]


@contextlib.contextmanager
def count_global_sqlite_queries():
    """拦截模块内所有 sqlite3.connect（含 TestClient 跨线程调用），精确统计单次 HTTP 请求 SQL 条数。"""
    counts = {"total": 0}
    orig_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        conn = orig_connect(*args, **kwargs)

        def _trace(stmt: str) -> None:
            upper = stmt.strip().upper()
            if upper.startswith(("SELECT", "INSERT", "UPDATE", "DELETE")):
                counts["total"] += 1

        conn.set_trace_callback(_trace)
        return conn

    sqlite3.connect = traced_connect
    try:
        yield counts
    finally:
        sqlite3.connect = orig_connect


def _seed_batch_with_stores(
    materials: MaterialService,
    src_root: Path,
    *,
    batch_name: str,
    store_count: int,
    project_id: str,
    confirm_all: bool = True,
) -> tuple[str, list[str]]:
    """快速构造指定门店数的已分析批次（直接落盘 64x64 测试 PNG + material_decisions，毫秒级建好）。"""
    src_root.mkdir(parents=True, exist_ok=True)
    # 预生成 1 份合法 PNG 字节，写入各门店原图（避免建夹具本身耗费时间）
    sample_png = src_root / "_template.png"
    Image.new("RGB", (120, 120), color=(240, 240, 240)).save(sample_png, format="PNG", compress_level=1)
    raw_bytes = sample_png.read_bytes()
    sample_png.unlink()
    digest = hashlib.sha256(raw_bytes).hexdigest()

    batch_id = hashlib.sha256(f"{batch_name}:{store_count}".encode()).hexdigest()[:32]
    stamp = now()
    store_ids: list[str] = []
    with materials.task_store.connect() as db:
        db.execute(
            "INSERT INTO material_batches(id,source_kind,source_root,status,progress_done,progress_total,"
            "snapshot_id,error,created_at,updated_at,name,description,deleted_at,project_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch_id, "local", str(src_root), "completed", store_count, store_count,
             "", "", stamp, stamp, batch_name, "", "", project_id),
        )
        rows = []
        for idx in range(store_count):
            folder = f"{idx + 1:04d}_{batch_name}_门店{idx + 1}"
            store_dir = src_root / folder
            store_dir.mkdir(parents=True, exist_ok=True)
            img_path = store_dir / "qr.png"
            img_path.write_bytes(raw_bytes)
            sid = hashlib.sha256(folder.encode()).hexdigest()[:24]
            store_ids.append(sid)
            rel_img = f"{folder}/qr.png"
            store_obj = {
                "store_id": sid,
                "source_name": folder,
                "source_folder": folder,
                "analysis_status": "selected",
                "reason": "高置信点餐码",
                "recommendation": True,
                "images": [{
                    "path": rel_img,
                    "sha256": digest,
                    "detection_count": 1,
                    "candidates": [{
                        "box": {"x": 20, "y": 20, "width": 60, "height": 60},
                        "payload_sha256": digest,
                        "short_edge_px": 60,
                        "selected": True,
                        "purpose": "ordering",
                        "purpose_reason": "测试点餐码",
                        "source": "local_detector",
                        "purpose_source": "payload_url",
                    }],
                }],
            }
            selected_obj = {"image_index": 0, "candidate_index": 0, "box": {"x": 20, "y": 20, "width": 60, "height": 60}}
            decision = "confirmed" if confirm_all else "pending"
            rows.append((
                batch_id, sid, folder, folder, "selected", 1, decision,
                json.dumps(selected_obj) if confirm_all else "{}",
                digest if confirm_all else "",
                json.dumps(store_obj, ensure_ascii=False),
                stamp, "", "", "", "",
            ))
        db.executemany(
            "INSERT INTO material_decisions(batch_id,store_id,source_name,source_folder,analysis_status,"
            "recommendation,decision,selected_json,selected_source_hash,store_json,updated_at,"
            "reject_reason,reject_detail,rejected_at,reject_reviewed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return batch_id, store_ids


def test_hub_endpoints_strict_sql_query_budgets(tmp_path: Path) -> None:
    """多批次 + 120 家门店规模下，核心 Hub 读接口必须满足固定 SQL 条数预算，严禁 N+1 回归。"""
    home = tmp_path / "hub"
    app = create_app(home, role="hub", admin_password="secret")
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    auth = ("admin", "secret")
    token = client.get("/api/session", auth=auth).json()["token"]
    headers = {"x-capture-token": token}

    proj_resp = client.post(
        "/api/projects", json={"code": "PERF01", "name": "性能基线项目"}, auth=auth, headers=headers,
    )
    assert proj_resp.status_code in {200, 201}, proj_resp.text
    proj_data = proj_resp.json()
    pid = (proj_data.get("project") or proj_data)["id"]

    store = TaskStore(home / "data" / "tasks.sqlite3")
    configs = ConfigStore(home / "data" / "config.json")
    materials = MaterialService(home, configs, store, role="hub")

    # 造 4 个批次 × 30 家店 = 120 家门店并全部发布，制造真实多批次压力
    published_task_ids: list[str] = []
    for i in range(4):
        bid, _sids = _seed_batch_with_stores(
            materials, tmp_path / f"src_{i}", batch_name=f"批次{i + 1}", store_count=30, project_id=pid,
        )
        materials.publish_snapshot(bid, "基线发布", name=f"批次{i + 1}", version="v1.0.0", project_id=pid)

    # 把其中 40 家标为待检查（needs_review），40 家标为不合格（rejected）
    effective = materials.effective_capture_tasks(project_id=pid)
    published_task_ids = [row["id"] for row in effective]
    assert len(published_task_ids) == 120
    with store.connect() as db:
        for tid in published_task_ids[:40]:
            db.execute("UPDATE tasks SET status='needs_review',stage='capture_failed',message='超时' WHERE id=?", (tid,))
        for tid in published_task_ids[40:80]:
            db.execute(
                "UPDATE tasks SET status='needs_review',reject_reason='expired_qr',rejected_at=? WHERE id=?",
                (now(), tid),
            )

    # 核心接口 SQL 条数硬预算（若任一接口退化为 per-batch / per-store N+1 会直接超标失败）
    budgets = [
        ("GET /api/materials/batches", f"/api/materials/batches?project_id={pid}", 16),
        ("GET /api/materials/rework", f"/api/materials/rework?project_id={pid}", 12),
        ("GET /api/reviews/pending", f"/api/reviews/pending?project_id={pid}", 15),
        ("GET /api/rejections", f"/api/rejections?project_id={pid}", 22),
        ("GET /api/rejections/export-store-names/preview", f"/api/rejections/export-store-names/preview?project_id={pid}", 28),
        ("GET /api/shared/status", f"/api/shared/status?project_id={pid}", 16),
        ("GET /api/shared/assignments", f"/api/shared/assignments?project_id={pid}", 10),
    ]
    for label, url, max_sql in budgets:
        with count_global_sqlite_queries() as counter:
            resp = client.get(url, auth=auth, headers=headers)
        assert resp.status_code == 200, f"{label} returned {resp.status_code}"
        assert counter["total"] <= max_sql, (
            f"性能回归：{label} 执行了 {counter['total']} 条 SQL，超出预算上限 {max_sql} 条"
        )


def test_worker_rejections_uses_covering_index_and_fast_path(tmp_path: Path) -> None:
    """Worker 端大 run_actions 表下，store.rejections() 必须走覆盖索引且不触发慢相关子查询。"""
    store = TaskStore(tmp_path / "data" / "tasks.sqlite3")
    configs = ConfigStore(tmp_path / "data" / "config.json")
    MaterialService(tmp_path, configs, store, role="worker")
    stamp = now()
    with store.connect() as db:
        db.execute(
            "INSERT INTO runs(id,mode,status,task_ids,created_at,updated_at,material_batch_id) "
            "VALUES('r1','auto','completed','[]',?,?, '')",
            (stamp, stamp),
        )
        for i in range(60):
            tid = f"t{i:04d}"
            db.execute(
                "INSERT INTO tasks(id,source_name,source_path,source_hash,status,stage,updated_at,reject_reason,rejected_at) "
                "VALUES(?,?,?,?, 'needs_review','failed',?, 'expired_qr', ?)",
                (tid, f"门店{i}", f"/tmp/s/{i}.png", "a" * 64, stamp, stamp),
            )
            # 每家店 20 条动作记录 = 1200 行 run_actions
            db.executemany(
                "INSERT INTO run_actions(run_id,task_id,attempt_id,stage,step,action,started_at,elapsed_ms,result,details) "
                "VALUES('r1',?,'att1','nav',?,'tap',?,10,'ok','{}')",
                [(tid, step, stamp) for step in range(20)],
            )
        plan = [
            row["detail"]
            for row in db.execute(
                "EXPLAIN QUERY PLAN SELECT latest.run_id FROM run_actions latest "
                "WHERE latest.task_id='t0001' ORDER BY latest.id DESC LIMIT 1"
            )
        ]
    assert any("COVERING INDEX run_actions_task_run" in line for line in plan), f"未命中覆盖索引：{plan}"

    with count_sql(store) as counter:
        rows = store.rejections()
    assert len(rows) == 60
    assert counter["total"] <= 5, f"Worker store.rejections() 执行了 {counter['total']} 条 SQL，超出上限 5"


def test_100_store_publish_no_duplicate_hashing_and_incremental_skips_unchanged_decode(
    tmp_path: Path, monkeypatch,
) -> None:
    """100 家门店发布：每张原图最多哈希 1 次；增量发布 2 家门店时其余 98 家不得触发 PIL 像素重解码。"""
    from menu_capture.projects.service import LEGACY_ID

    home = tmp_path / "hub"
    store = TaskStore(home / "data" / "tasks.sqlite3")
    configs = ConfigStore(home / "data" / "config.json")
    materials = MaterialService(home, configs, store, role="standalone")

    bid, sids = _seed_batch_with_stores(
        materials, tmp_path / "src100", batch_name="百店批次", store_count=100, project_id=LEGACY_ID,
    )

    # 1. 首次 100 家全量发布：每张原图 SHA-256 最多算 1 次（不能退化为 4 次！）
    real_sha = materials_mod._sha256
    src_hash_calls: dict[str, int] = {}

    def spy_sha(path: Path) -> str:
        key = str(path)
        if "src100" in key:
            src_hash_calls[key] = src_hash_calls.get(key, 0) + 1
        return real_sha(path)

    monkeypatch.setattr(materials_mod, "_sha256", spy_sha)
    materials.publish_snapshot(bid, "首版发布", name="百店批次", version="v1.0.0", project_id=LEGACY_ID)
    assert len(src_hash_calls) == 100
    assert max(src_hash_calls.values()) == 1, f"原图被重复哈希：{max(src_hash_calls.values())} 次"

    # 2. 真正修改其中 2 家门店的选码框后增量发布：其余 98 家未改动门店不得触发 pixel_digest 的 PIL 重解码！
    with store.connect() as db:
        for sid in sids[:2]:
            new_sel = json.dumps({"image_index": 0, "candidate_index": 0, "box": {"x": 22, "y": 22, "width": 58, "height": 58}})
            db.execute("UPDATE material_decisions SET selected_json=? WHERE batch_id=? AND store_id=?", (new_sel, bid, sid))
    decode_calls = {"n": 0}
    real_pixel_digest = smv_mod.pixel_digest

    def spy_pixel_digest(path: Path) -> str:
        decode_calls["n"] += 1
        return real_pixel_digest(path)

    monkeypatch.setattr(smv_mod, "pixel_digest", spy_pixel_digest)
    materials.publish_snapshot(bid, "增量2家", store_ids=sids[:2], activate=False, project_id=LEGACY_ID)
    assert decode_calls["n"] == 0, f"增量发布触发了 {decode_calls['n']} 次多余的 PIL pixel_digest 解码"

    # 3. 批量打回 20 家门店：单次事务完成，避免 20 次全量扫描
    with count_sql(store) as counter:
        res = materials.return_rework_stores([(bid, sid) for sid in sids[:20]], project_id=LEGACY_ID)
    assert res["returned"] == 20
    assert not res["errors"]
    assert counter["total"] <= 125, f"批量打回 20 家门店执行了 {counter['total']} 条 SQL，超出预算"


def test_large_dataset_status_endpoints_stay_under_50kb_and_bulk_assign_avoids_n_plus_1(tmp_path: Path) -> None:
    """千级任务下轻量轮询与分页接口响应体必须 < 50KB；200 家门店批量分配/回收严禁 N+1 SQL。"""
    from menu_capture.shared.shared import SharedHub

    home = tmp_path / "hub-large"
    app = create_app(home, role="hub", admin_password="secret")
    auth = ("admin", "secret")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/api/session", auth=auth).json()["token"]
        headers = {"x-capture-token": token}

        proj_resp = client.post(
            "/api/projects", json={"code": "LARGE1", "name": "大数据量压测项目"}, auth=auth, headers=headers,
        )
        assert proj_resp.status_code in {200, 201}, proj_resp.text
        proj_data = proj_resp.json()
        pid = (proj_data.get("project") or proj_data)["id"]

        store = app.state.store
        materials = app.state.materials
        hub = SharedHub(store, materials)

        # 1) 造 200 家真实已发布门店用于批量分配/回收 SQL 预算验证
        bid, _sids = _seed_batch_with_stores(
            materials, tmp_path / "src200", batch_name="二百店批次", store_count=200, project_id=pid,
        )
        materials.publish_snapshot(bid, "首版", name="二百店批次", version="v1.0.0", project_id=pid)
        effective = materials.effective_capture_tasks(project_id=pid)
        assert len(effective) == 200
        task_ids = [row["id"] for row in effective]

        # 配对一台支持 project_scope 的采集端
        code = hub.pairing_code()["code"]
        paired = hub.pair(code, "压测机A")
        worker_id = paired.get("worker_id") or paired["id"]
        with store.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO shared_worker_capabilities(worker_id,capabilities,updated_at) VALUES(?,?,?)",
                (worker_id, json.dumps(["project_scope", "run_history", "run_summary", "software_status"]), now()),
            )

        # 200 家门店批量分配：消除 N+1 后除 200 条 INSERT 外，其余 SELECT/UPDATE 必须为常数级（<= 20）
        with count_sql(store) as assign_sql:
            assigned = hub.assign(worker_id, task_ids, project_id=pid)
        assert len(assigned["assignments"]) == 200
        non_insert = [q for q in assign_sql["queries"] if not q.upper().startswith("INSERT")]
        assert len(non_insert) <= 20, f"200 店批量分配执行了 {len(non_insert)} 条非 INSERT SQL，存在 N+1 回归"
        assert assign_sql["total"] <= 225, f"200 店批量分配总 SQL={assign_sql['total']}，超出预算 225"

        # 200 家门店批量回收：常数级 SQL（<= 15）
        assignment_ids = [row["id"] for row in assigned["assignments"]]
        with count_sql(store) as reclaim_sql:
            reclaimed = hub.reclaim_many(assignment_ids, project_id=pid)
        assert reclaimed["count"] == 200
        assert reclaim_sql["total"] <= 15, f"200 店批量回收执行了 {reclaim_sql['total']} 条 SQL，超出预算 15"

        # 2) 模拟 1600 家门店场景，验证轻量状态轮询与分页任务查询响应体均 < 50KB
        synthetic_1600 = [
            {
                "id": f"task-{i:04d}",
                "source_name": f"E{i:04d}_大数据量压测门店名称_{i}",
                "source_path": f"/data/materials/published/snap-01/source/E{i:04d}.png",
                "source_hash": "a" * 64,
                "status": "needs_review" if i % 7 == 0 else ("captured" if i % 3 == 0 else "pending"),
                "stage": "captured" if i % 3 == 0 else "pending",
                "message": "已完成截图并等待审核" if i % 3 == 0 else "",
                "error_code": "",
                "reject_reason": "qr_expired" if i % 11 == 0 else "",
                "reject_detail": "",
                "output_dir": "",
                "updated_at": now(),
                "batch_id": bid,
                "batch_name": "二百店批次",
                "version": "v1.0.0",
                "snapshot_id": "snap-01",
                "batch_snapshot_id": "snap-01",
                "runnable": i % 3 != 0 and i % 11 != 0,
            }
            for i in range(1600)
        ]
        orig_effective = materials.effective_capture_tasks
        materials.effective_capture_tasks = lambda **_kw: synthetic_1600  # type: ignore[method-assign]
        try:
            summary_resp = client.get(f"/api/status/summary?project_id={pid}", auth=auth, headers=headers)
            assert summary_resp.status_code == 200, summary_resp.text
            assert len(summary_resp.content) < 50_000, f"/api/status/summary 体积过大：{len(summary_resp.content)} bytes"
            assert summary_resp.json()["tasks"] == []
            assert sum(summary_resp.json()["counts"][k] for k in ("rejected", "returned", "review", "captured", "running", "pending")) == 1600

            paged_resp = client.get(
                f"/api/status?queue_page=1&queue_size=20&include_events=false&project_id={pid}",
                auth=auth, headers=headers,
            )
            assert paged_resp.status_code == 200
            assert len(paged_resp.content) < 50_000, f"/api/status 分页响应过大：{len(paged_resp.content)} bytes"
            assert len(paged_resp.json()["tasks"]) == 20
            assert paged_resp.json()["queue"]["total"] == 1600

            tasks_resp = client.get(
                f"/api/status/tasks?page=1&size=20&project_id={pid}",
                auth=auth, headers=headers,
            )
            assert tasks_resp.status_code == 200
            assert len(tasks_resp.content) < 50_000, f"/api/status/tasks 体积过大：{len(tasks_resp.content)} bytes"
            assert len(tasks_resp.json()["records"]) == 20
            assert tasks_resp.json()["total"] == 1600
        finally:
            materials.effective_capture_tasks = orig_effective  # type: ignore[method-assign]
