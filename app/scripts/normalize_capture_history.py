#!/usr/bin/env python3
"""Plan/apply a conservative, journaled normalization of historical menu captures.

Planning is read-only. Applying a saved plan requires the runner lock to be free,
backs up SQLite, and moves directories with same-filesystem atomic renames.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from menu_capture.capture_outputs import store_output_dir  # noqa: E402

AUDIT_CUTOFF = "2026-11-08T21:09:00+08:00"
ACCEPTED = {
    "E00510＿丰圆轩粤式茶楼＿上海市＿丰圆轩粤式茶楼（静安大融城店）": 34,
    "E00887＿龙师傅卤炒＿长沙市＿龙师傅卤炒（旺龙路店）": 17,
    "E00919＿小厨快炒＿长沙市＿彭满哥鲜肉粉": 24,
}


def connect(db_path: Path, readonly: bool = True):
    mode = "ro" if readonly else "rw"
    db = sqlite3.connect(f"file:{db_path}?mode={mode}", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def valid_pages(directory: Path) -> tuple[bool, list[dict]]:
    pages = sorted(p for p in directory.iterdir() if p.is_file() and re.fullmatch(r"page-\d{4}\.png", p.name))
    if not pages or [p.name for p in pages] != [f"page-{i:04d}.png" for i in range(1, len(pages) + 1)]:
        return False, []
    evidence = []
    try:
        from PIL import Image
        for page in pages:
            with Image.open(page) as image:
                image.verify()
            with Image.open(page) as image:
                image.load()
            evidence.append({"name": page.name, "size": page.stat().st_size, "sha256": digest_file(page), "valid_png": True})
    except Exception as exc:
        return False, [{"error": f"PNG 校验失败: {type(exc).__name__}"}]
    return True, evidence


def table_rows(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args)]


def make_plan(db_path: Path, output_root: Path, plan_id: str) -> dict:
    db = connect(db_path)
    try:
        tasks = table_rows(db, "SELECT * FROM tasks WHERE output_dir != ''")
        task_by_id = {row["id"]: row for row in tasks}
        published = {(r["batch_id"], r["snapshot_id"]) for r in db.execute(
            "SELECT batch_id,snapshot_id FROM material_versions"
        )}
        actions = table_rows(db, """SELECT a.id action_id,a.task_id,a.run_id,a.attempt_id,a.started_at,a.result,
            r.material_batch_id,r.material_version,r.material_snapshot,r.status run_status
            FROM run_actions a JOIN runs r ON r.id=a.run_id
            WHERE a.action='publish_output' AND a.result='ok' ORDER BY a.started_at,a.id""")
        latest: dict[str, dict] = {}
        for action in actions:
            task = task_by_id.get(action["task_id"])
            if task and task["output_dir"]:
                dest = os.path.abspath(task["output_dir"])
                if Path(dest).parent == output_root.resolve():
                    latest[dest] = {**action, "task": task}

        entries = []
        for src in sorted(output_root.iterdir()):
            if src.name.startswith(".") or src.name in {"清洗批次", "诊断"} or not src.is_dir():
                continue
            assert_no_symlink_chain(src, output_root)
            snapshot = file_snapshot(src)
            record = latest.get(str(src.resolve()))
            reasons = []
            classification = "archive"
            batch_id = ""
            task_refs = []
            run_evidence = None
            files = []
            if record:
                task = record["task"]
                batch_id = record["material_batch_id"] or ""
                task_refs = [{"id": task["id"], "source_name": task["source_name"], "source_hash": task["source_hash"], "source_path": task["source_path"], "status": task["status"], "output_dir": task["output_dir"]}]
                run_evidence = {k: record[k] for k in ("action_id", "run_id", "attempt_id", "started_at", "material_batch_id", "material_version", "material_snapshot", "run_status")}
                exact = task["source_name"] == src.name and task["output_dir"] == str(src)
                path_snapshot = Path(task["source_path"]).parent.parent.name
                snapshot_match = ((batch_id, path_snapshot) in published and path_snapshot == record["material_snapshot"])
                expected_pages = ACCEPTED.get(task["source_name"])
                if expected_pages:
                    pages_ok, files = valid_pages(src)
                else:
                    pages_ok = False
                cutoff_epoch = datetime.fromisoformat(AUDIT_CUTOFF).timestamp()
                no_later_fs_changes = all((src / f["name"]).stat().st_mtime <= cutoff_epoch for f in files if "name" in f)
                audit_files_match = pages_ok and len(files) == expected_pages and all(
                    name == ".DS_Store" or re.fullmatch(r"page-\d{4}\.png", name) for name, _, _ in snapshot
                )
                if exact and snapshot_match and record["started_at"] <= AUDIT_CUTOFF and audit_files_match and no_later_fs_changes:
                    classification = "formal"
                    reasons.append("审计认可门店；发布、来源快照与目录匹配且不晚于审计快照；页码连续且 PNG 完整可解码")
                else:
                    if not exact: reasons.append("发布任务名称或输出路径与可见目录不精确匹配")
                    if not snapshot_match: reasons.append("来源路径快照与 run.material_snapshot 不一致或缺失")
                    if not expected_pages: reasons.append("历史审计未确认达到交付标准")
                    if record["started_at"] > AUDIT_CUTOFF: reasons.append("发布晚于 2026-11-08 21:09 审计快照，审计不能证明该版本")
                    if expected_pages and not audit_files_match: reasons.append("文件与审计页数或完整性不符")
                    if pages_ok and not no_later_fs_changes: reasons.append("文件修改时间晚于审计快照，无法证明是审计时版本")
                    reasons.append("归档待复核；发布成功不等同于业务验收通过")
            else:
                reasons.append("当前 DB 没有匹配的成功 publish_output 记录")
                exact = snapshot_match = False
            if not (exact and snapshot_match and re.fullmatch(r"[0-9a-f]{32}", batch_id)):
                batch_id = "归属待核验"
            if classification == "formal":
                dest = Path(store_output_dir(output_root, src.name, batch_id))
            else:
                dest = output_root / ".历史截图" / plan_id / "待复核" / batch_id / src.name
            entries.append({"source": str(src), "destination": str(dest), "classification": classification,
                            "batch_id": batch_id, "reasons": reasons, "tasks": task_refs,
                            "run": run_evidence, "files": files, "snapshot": snapshot, "state": "planned"})
        # Visible diagnostics are preserved as an archive item, with no inferred batch.
        diag = output_root / "诊断"
        if diag.exists():
            assert_no_symlink_chain(diag, output_root)
            entries.append({"source": str(diag), "destination": str(output_root / ".历史截图" / plan_id / "待复核" / "归属待核验" / "诊断"),
                            "classification": "diagnostics", "batch_id": "归属待核验", "reasons": ["诊断材料单独归档，不推断门店批次"],
                            "tasks": [], "run": None, "files": [], "snapshot": file_snapshot(diag), "state": "planned"})
        return {"schema": 1, "plan_id": plan_id, "created_at": datetime.now().astimezone().isoformat(),
                "workspace": str(ROOT.parent.parent), "db_path": str(db_path), "output_root": str(output_root),
                "audit_cutoff": AUDIT_CUTOFF, "status": "planned", "entries": entries}
    finally:
        db.close()


def assert_no_symlink_chain(path: Path, stop: Path):
    cur = path
    while True:
        if cur.is_symlink():
            raise RuntimeError(f"路径含符号链接，停止: {cur}")
        if cur == stop or cur.parent == cur:
            break
        cur = cur.parent


def file_snapshot(directory: Path) -> list[tuple[str, int, str]]:
    result = []
    for p in sorted(directory.rglob("*")):
        if p.is_symlink(): raise RuntimeError(f"源目录含符号链接，停止: {p}")
        if p.is_file(): result.append((str(p.relative_to(directory)), p.stat().st_size, digest_file(p)))
    return result


def apply_plan(plan_path: Path):
    plan_path = plan_path.resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema") != 1 or plan.get("status") not in ("planned", "applying", "interrupted"):
        raise RuntimeError("计划格式/状态不支持")
    if not re.fullmatch(r"[a-zA-Z0-9-]+", plan["plan_id"]):
        raise RuntimeError("计划编号无效")
    root = Path(plan["output_root"]).resolve()
    db_path = Path(plan["db_path"]).resolve()
    lock_path = db_path.parent / "runner.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError("runner.lock 被占用；服务必须离线")
        with connect(db_path) as db:
            if db.execute("SELECT 1 FROM publish_transactions LIMIT 1").fetchone():
                raise RuntimeError("尚有发布事务待恢复")
            if db.execute("SELECT 1 FROM tasks WHERE status='running' LIMIT 1").fetchone():
                raise RuntimeError("尚有运行中任务")
            if db.execute("SELECT 1 FROM runs WHERE status IN ('running','pause_requested','resuming','terminating') LIMIT 1").fetchone():
                raise RuntimeError("尚有运行中批次")
        # DB backup is done before any filesystem mutation and only once per plan.
        backup = db_path.with_name(f"{db_path.stem}.pre-normalize-{plan['plan_id']}{db_path.suffix}")
        if not plan.get("backup_path"):
            if backup.exists(): raise RuntimeError(f"备份路径已存在，拒绝覆盖: {backup}")
            with connect(db_path) as source, sqlite3.connect(backup) as target: source.backup(target)
            plan["backup_path"] = str(backup)
            plan["status"] = "applying"
            save_plan(plan_path, plan)
        elif not backup.is_file():
            raise RuntimeError("计划记录的 SQLite 备份不存在")

        # Preflight all outstanding moves before touching any of them.
        for item in plan["entries"]:
            src, dst = Path(item["source"]), Path(item["destination"])
            if src.parent != root or not dst.is_relative_to(root) or ".." in dst.parts:
                raise RuntimeError(f"计划路径越界: {src} -> {dst}")
            assert_no_symlink_chain(src, root)
            assert_no_symlink_chain(dst, root)
            if item["classification"] == "formal":
                expected = Path(store_output_dir(root, item["tasks"][0]["source_name"], item["batch_id"]))
            elif item["classification"] == "diagnostics":
                expected = root / ".历史截图" / plan["plan_id"] / "待复核" / "归属待核验" / "诊断"
            elif item["classification"] == "archive":
                expected = root / ".历史截图" / plan["plan_id"] / "待复核" / item["batch_id"] / src.name
            else:
                raise RuntimeError("计划含未知分类")
            if dst != expected:
                raise RuntimeError(f"计划目标与分类规则不符: {dst}")
            if item["state"] == "moved":
                if not dst.exists() or file_snapshot(dst) != [tuple(v) for v in item["snapshot"]]:
                    raise RuntimeError(f"已迁移目标与日志不符: {dst}")
                update_task_pointers(db_path, item)
                continue
            # Recover a crash after rename but before the journal/DB commit.
            if not src.exists() and dst.is_dir():
                if file_snapshot(dst) != [tuple(v) for v in item["snapshot"]]:
                    raise RuntimeError(f"中断恢复时目标内容与计划不符: {dst}")
                item["state"] = "moved"
                save_plan(plan_path, plan)
                update_task_pointers(db_path, item)
                continue
            if not src.exists() or not src.is_dir(): raise RuntimeError(f"来源不存在: {src}")
            if dst.exists(): raise RuntimeError(f"目标已存在，拒绝覆盖: {dst}")
            if src.stat().st_dev != root.stat().st_dev: raise RuntimeError(f"不是同一文件系统，无法原子迁移: {src}")
            if file_snapshot(src) != [tuple(v) for v in item["snapshot"]]:
                raise RuntimeError(f"目录内容已变化，请重新生成计划: {src}")
            if item["classification"] == "formal":
                task = item["tasks"][0]
                with connect(db_path) as db:
                    current = db.execute("SELECT source_path,source_hash,output_dir FROM tasks WHERE id=?", (task["id"],)).fetchone()
                if not current or any(current[field] != task[field] for field in ("source_path", "source_hash", "output_dir")):
                    raise RuntimeError(f"正式任务归属已变化，请重新生成计划: {src}")
        save_plan(plan_path, plan)

        for item in plan["entries"]:
            if item["state"] == "moved": continue
            src, dst = Path(item["source"]), Path(item["destination"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists(): raise RuntimeError(f"目标在迁移前出现，拒绝覆盖: {dst}")
            os.rename(src, dst)
            update_task_pointers(db_path, item)
            item["state"] = "moved"
            save_plan(plan_path, plan)
        plan["status"] = "complete"
        plan["completed_at"] = datetime.now().astimezone().isoformat()
        save_plan(plan_path, plan)


def update_task_pointers(db_path: Path, item: dict):
    src, dst = item["source"], item["destination"]
    with connect(db_path, readonly=False) as db:
        db.execute("BEGIN IMMEDIATE")
        stamp = datetime.now().astimezone().isoformat()
        if item["classification"] == "formal":
            task = item["tasks"][0]
            snapshots = {row["snapshot_id"] for row in db.execute(
                "SELECT snapshot_id FROM material_versions WHERE batch_id=?", (item["batch_id"],)
            )}
            # 同批次其他版本只在二维码一致时保留已确认的产物引用。
            for row in db.execute("SELECT id,source_hash,source_path FROM tasks WHERE output_dir=?", (src,)).fetchall():
                if row["source_hash"] == task["source_hash"] and Path(row["source_path"]).parent.parent.name in snapshots:
                    db.execute("UPDATE tasks SET output_dir=? WHERE id=?", (dst, row["id"]))
        db.execute("""UPDATE tasks SET output_dir='',
            stage=CASE WHEN status='captured' THEN 'output_review' ELSE stage END,
            message=CASE WHEN status='captured' THEN '历史截图归档或归属待复核，请确认后重新采集' ELSE message END,
            status=CASE WHEN status='captured' THEN 'needs_review' ELSE status END,
            updated_at=CASE WHEN status='captured' THEN ? ELSE updated_at END
            WHERE output_dir=?""", (stamp, src))
        db.commit()


def save_plan(path: Path, plan: dict):
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="只读生成 JSON 迁移计划")
    p.add_argument("--output", type=Path, required=True, help="计划 JSON 输出路径")
    p.add_argument("--root", type=Path, default=ROOT.parent.parent / "输出/截图输出")
    p.add_argument("--db", type=Path, default=ROOT / "data/tasks.sqlite3")
    p.add_argument("--plan-id", default="")
    a = sub.add_parser("apply", help="应用一份已审阅计划")
    a.add_argument("plan", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            pid = args.plan_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
            if not re.fullmatch(r"[a-zA-Z0-9-]+", pid):
                raise RuntimeError("计划编号无效")
            plan = make_plan(args.db.resolve(), args.root.resolve(), pid)
            if args.output.exists(): raise RuntimeError(f"计划文件已存在，拒绝覆盖: {args.output}")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            save_plan(args.output, plan)
            print(json.dumps({"plan": str(args.output.resolve()), "entries": len(plan["entries"]),
                              "formal": sum(x["classification"] == "formal" for x in plan["entries"]),
                              "archive": sum(x["classification"] == "archive" for x in plan["entries"]),
                              "diagnostics": sum(x["classification"] == "diagnostics" for x in plan["entries"])}, ensure_ascii=False))
        else:
            apply_plan(args.plan)
            print(f"迁移完成，计划与逐项状态已记录: {args.plan.resolve()}")
    except Exception as exc:
        print(f"失败: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
