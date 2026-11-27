"""一键系统级性能基准审计脚本（Hub + Worker + 100 家门店批量操作）。

用法：
    cd app && PYTHONPATH=. python scripts/perf_audit.py

输出：
- Hub 全量核心读接口耗时、SQL 条数、响应字节数
- Worker 端大 run_actions 下 rejections / review_candidates 耗时与索引命中
- 100 家门店全量发布、2 家跨批次增量发布、20 家批量打回耗时与 SQL 条数
"""
from __future__ import annotations

import cProfile
import io
import pstats
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from menu_capture.api.server import create_app
from menu_capture.core.config import ConfigStore
from menu_capture.materials.materials import MaterialService
from menu_capture.storage.store import TaskStore, now
from tests.perf.test_perf_regression import (
    _seed_batch_with_stores,
    count_global_sqlite_queries,
    count_sql,
)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mc-perf-audit-") as tmp:
        root = Path(tmp)
        home = root / "hub"
        app = create_app(home, role="hub", admin_password="secret")
        client = TestClient(app, base_url="http://127.0.0.1:8765")
        auth = ("admin", "secret")
        token = client.get("/api/session", auth=auth).json()["token"]
        headers = {"x-capture-token": token}
        proj_data = client.post(
            "/api/projects", json={"code": "BENCH1", "name": "基准项目"}, auth=auth, headers=headers,
        ).json()
        pid = (proj_data.get("project") or proj_data)["id"]

        store = TaskStore(home / "data" / "tasks.sqlite3")
        configs = ConfigStore(home / "data" / "config.json")
        materials = MaterialService(home, configs, store, role="hub")

        # 1. 100 家门店全量发布基准（含 cProfile 热点）
        bid100, sids100 = _seed_batch_with_stores(
            materials, root / "src100", batch_name="百店批次", store_count=100, project_id=pid,
        )
        profiler = cProfile.Profile()
        t0 = time.monotonic()
        profiler.enable()
        materials.publish_snapshot(bid100, "百店首版", name="百店批次", version="v1.0.0", project_id=pid)
        profiler.disable()
        pub100_ms = (time.monotonic() - t0) * 1000

        # 2. 增量发布 2 家门店基准
        t1 = time.monotonic()
        materials.publish_snapshot(bid100, "增量2家", store_ids=sids100[:2], activate=False, project_id=pid)
        inc2_ms = (time.monotonic() - t1) * 1000

        # 3. 批量打回 20 家门店基准
        with count_sql(store) as ret_sql:
            t2 = time.monotonic()
            materials.return_rework_stores([(bid100, sid) for sid in sids100[:20]], project_id=pid)
            ret20_ms = (time.monotonic() - t2) * 1000

        # 4. 补造 3 个小批次用于多批次 API 压力
        for idx in range(3):
            bid, _ = _seed_batch_with_stores(
                materials, root / f"src_extra_{idx}", batch_name=f"扩展批次{idx + 1}", store_count=20, project_id=pid,
            )
            materials.publish_snapshot(bid, "首版", name=f"扩展批次{idx + 1}", version="v1.0.0", project_id=pid)

        effective = materials.effective_capture_tasks(project_id=pid)
        stamp = now()
        with store.connect() as db:
            for row in effective[:40]:
                db.execute("UPDATE tasks SET status='needs_review',stage='failed',message='超时' WHERE id=?", (row["id"],))
            for row in effective[40:80]:
                db.execute(
                    "UPDATE tasks SET status='needs_review',reject_reason='expired_qr',rejected_at=? WHERE id=?",
                    (stamp, row["id"]),
                )

        print("### 批量写操作基准（100 家门店）")
        print(f"- 100 家门店首次全量发布: **{pub100_ms:.1f} ms**")
        print(f"- 100 家批次增量发布 2 家: **{inc2_ms:.1f} ms**")
        print(f"- 批量打回 20 家门店: **{ret20_ms:.1f} ms** ({ret_sql['total']} SQLs)\n")

        print("### Hub 核心读接口基准（4 批次 / 160 家门店）")
        print(f"{'Endpoint':<48} {'Time(ms)':>9} {'SQLs':>6} {'Bytes':>9}")
        print("-" * 76)
        for label, url in [
            ("GET /api/materials/batches", f"/api/materials/batches?project_id={pid}"),
            ("GET /api/materials/rework", f"/api/materials/rework?project_id={pid}"),
            ("GET /api/reviews/pending", f"/api/reviews/pending?project_id={pid}"),
            ("GET /api/rejections", f"/api/rejections?project_id={pid}"),
            ("GET /api/rejections/export-store-names/preview", f"/api/rejections/export-store-names/preview?project_id={pid}"),
            ("GET /api/shared/status", f"/api/shared/status?project_id={pid}"),
            ("GET /api/shared/assignments", f"/api/shared/assignments?project_id={pid}"),
        ]:
            with count_global_sqlite_queries() as counter:
                ts = time.monotonic()
                resp = client.get(url, auth=auth, headers=headers)
                ms = (time.monotonic() - ts) * 1000
            print(f"{label:<48} {ms:>9.1f} {counter['total']:>6} {len(resp.content):>9}")

        print("\n### 100 家门店发布 cProfile Top-8 热点")
        buf = io.StringIO()
        pstats.Stats(profiler, stream=buf).sort_stats("cumulative").print_stats(8)
        print(buf.getvalue())


if __name__ == "__main__":
    main()
