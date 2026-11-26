"""Project identity and explicit scope primitives (Refs #102).

The integrated service owns startup migration and supplies its write transaction.
"""
from datetime import datetime, timezone
import re
import sqlite3
import uuid

LEGACY_ID = "00000000000000000000000000000102"
_FIELDS = "id,code,name,description,created_at,updated_at,archived_at"
_RESERVED = {"legacy", "con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class ProjectNotFound(ValueError):
    pass


class ProjectConflict(ValueError):
    pass


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _require_write(db):
    if not db.in_transaction:
        raise RuntimeError("project writes require an explicit transaction")


def migrate_projects(db: sqlite3.Connection) -> None:
    """Add and validate project ownership inside a caller-owned transaction.

    Only the first migration backfills legacy batches; later unscoped rows are
    errors, not silently classified. The old selection is read exactly once.
    """
    _require_write(db)
    db.execute("""CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE COLLATE NOCASE,
        name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        archived_at TEXT NOT NULL DEFAULT '')""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(material_batches)")}
    if not columns:
        raise RuntimeError("material_batches must exist before project migration")
    first = "project_id" not in columns
    if first:
        db.execute("ALTER TABLE material_batches ADD COLUMN project_id TEXT REFERENCES projects(id)")
        if db.execute("SELECT 1 FROM material_batches LIMIT 1").fetchone():
            stamp = _stamp()
            db.execute("INSERT INTO projects(id,code,name,created_at,updated_at) VALUES(?,?,?,?,?)",
                       (LEGACY_ID, "legacy", "历史项目", stamp, stamp))
            db.execute("UPDATE material_batches SET project_id=?", (LEGACY_ID,))
    db.execute("""CREATE INDEX IF NOT EXISTS material_batches_project
        ON material_batches(project_id,deleted_at,updated_at)""")
    db.execute("""CREATE TABLE IF NOT EXISTS project_material_selection (
        project_id TEXT PRIMARY KEY REFERENCES projects(id),
        version_id TEXT NOT NULL REFERENCES material_versions(id))""")
    if first:
        db.execute("""INSERT INTO project_material_selection(project_id,version_id)
            SELECT b.project_id,s.version_id FROM material_selection s
            JOIN material_versions v ON v.id=s.version_id
            JOIN material_batches b ON b.id=v.batch_id
            WHERE s.singleton=1 AND b.deleted_at=''""")
        if db.execute("""SELECT 1 FROM material_selection s WHERE NOT EXISTS (
            SELECT 1 FROM project_material_selection p WHERE p.version_id=s.version_id)""").fetchone():
            raise ProjectConflict("历史素材选择无法解析，请先核验旧数据")
    if db.execute("""SELECT 1 FROM material_batches b LEFT JOIN projects p ON p.id=b.project_id
        WHERE p.id IS NULL LIMIT 1""").fetchone():
        raise ProjectConflict("存在未关联有效项目的素材批次")
    if db.execute("""SELECT 1 FROM project_material_selection s
        LEFT JOIN material_versions v ON v.id=s.version_id
        LEFT JOIN material_batches b ON b.id=v.batch_id
        LEFT JOIN projects p ON p.id=s.project_id
        WHERE p.id IS NULL OR b.id IS NULL OR b.project_id IS NOT s.project_id OR b.deleted_at!=''
        LIMIT 1""").fetchone():
        raise ProjectConflict("存在无效或跨项目的素材选择")
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "capture_run_results" in tables:
        result_columns = {row[1] for row in db.execute("PRAGMA table_info(capture_run_results)")}
        if {"project_id", "batch_id"}.issubset(result_columns):
            # 批次外键是逐店历史的直接证据；只回填可精确关联的旧记录。
            db.execute("""UPDATE capture_run_results SET project_id=(
                SELECT project_id FROM material_batches WHERE id=capture_run_results.batch_id)
                WHERE project_id='' AND batch_id<>'' AND EXISTS (
                    SELECT 1 FROM material_batches WHERE id=capture_run_results.batch_id
                      AND project_id IS NOT NULL AND project_id<>'')""")
    if "runs" in tables and "project_id" in {row[1] for row in db.execute("PRAGMA table_info(runs)")}:
        run_columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
        if "material_batch_id" in run_columns:
            db.execute("""UPDATE runs SET project_id=(
                SELECT project_id FROM material_batches WHERE id=runs.material_batch_id)
                WHERE project_id='' AND material_batch_id<>'' AND EXISTS (
                    SELECT 1 FROM material_batches WHERE id=runs.material_batch_id
                      AND project_id IS NOT NULL AND project_id<>'')""")
        if "capture_run_results" in tables:
            db.execute("""UPDATE runs SET project_id=(
                SELECT MIN(c.project_id) FROM capture_run_results c WHERE c.run_id=runs.id
                  AND c.project_id<>'')
                WHERE project_id='' AND EXISTS (
                    SELECT 1 FROM capture_run_results c WHERE c.run_id=runs.id)
                  AND NOT EXISTS (
                    SELECT 1 FROM capture_run_results c WHERE c.run_id=runs.id AND c.project_id='')
                  AND (SELECT COUNT(DISTINCT c.project_id) FROM capture_run_results c
                       WHERE c.run_id=runs.id AND c.project_id<>'')=1""")


def ensure_legacy_project(db: sqlite3.Connection) -> dict:
    """为仍以单工作空间运行的本机模式提供固定兼容项目。"""
    _require_write(db)
    if db.execute("SELECT 1 FROM projects WHERE id=?", (LEGACY_ID,)).fetchone() is None:
        stamp = _stamp()
        db.execute("INSERT INTO projects(id,code,name,created_at,updated_at) VALUES(?,?,?,?,?)",
                   (LEGACY_ID, "legacy", "历史项目", stamp, stamp))
    return get_project(db, LEGACY_ID)


def get_project(db, project_id: str) -> dict:
    row = db.execute(f"SELECT {_FIELDS} FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise ProjectNotFound("项目不存在")
    return dict(zip(_FIELDS.split(","), row))


def create_project(db, code: str, name: str, description: str = "") -> dict:
    _require_write(db)
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,48}", code) or code.lower() in _RESERVED:
        raise ValueError("项目标识无效或为保留名称")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError("项目名称必须为 1 至 80 个字符")
    if not isinstance(description, str) or len(description) > 500:
        raise ValueError("项目描述最多 500 字符")
    if db.execute("SELECT 1 FROM projects WHERE code=? COLLATE NOCASE", (code,)).fetchone():
        raise ProjectConflict("项目标识已存在")
    project_id, stamp = uuid.uuid4().hex, _stamp()
    db.execute("INSERT INTO projects(id,code,name,description,created_at,updated_at) VALUES(?,?,?,?,?,?)",
               (project_id, code, name.strip(), description.strip(), stamp, stamp))
    return get_project(db, project_id)


def assert_writable(db, project_id: str) -> dict:
    project = get_project(db, project_id)
    if project["archived_at"]:
        raise ProjectConflict("项目已归档")
    return project


def resolve_batch_project(db, batch_id: str, *, include_deleted: bool = False) -> dict:
    row = db.execute("SELECT project_id,deleted_at FROM material_batches WHERE id=?", (batch_id,)).fetchone()
    if row is None or (row[1] and not include_deleted):
        raise ProjectNotFound("素材批次不存在")
    return get_project(db, row[0])


def assert_batch_scope(db, project_id: str, batch_id: str, *, write: bool = False) -> dict:
    if write:
        _require_write(db)
    project = resolve_batch_project(db, batch_id)
    if project["id"] != project_id:
        raise ProjectNotFound("素材批次不在当前项目")
    if write:
        assert_writable(db, project_id)
    return project


def select_version(db, project_id: str, version_id: str) -> None:
    _require_write(db)
    row = db.execute("SELECT batch_id FROM material_versions WHERE id=?", (version_id,)).fetchone()
    if row is None:
        raise ProjectNotFound("素材版本不存在")
    assert_batch_scope(db, project_id, row[0], write=True)
    db.execute("""INSERT INTO project_material_selection(project_id,version_id) VALUES(?,?)
        ON CONFLICT(project_id) DO UPDATE SET version_id=excluded.version_id""", (project_id, version_id))


def list_projects(db, *, include_archived: bool = False) -> list[dict]:
    predicate = "" if include_archived else "WHERE archived_at=''"
    return [dict(zip(_FIELDS.split(","), row)) for row in db.execute(
        f"SELECT {_FIELDS} FROM projects {predicate} ORDER BY archived_at!='',name COLLATE NOCASE,id"
    )]


def update_project(db, project_id: str, *, name: str | None = None,
                   description: str | None = None) -> dict:
    _require_write(db)
    project = get_project(db, project_id)
    if name is not None and (not isinstance(name, str) or not 1 <= len(name.strip()) <= 80):
        raise ValueError("项目名称必须为 1 至 80 个字符")
    if description is not None and (not isinstance(description, str) or len(description) > 500):
        raise ValueError("项目描述最多 500 字符")
    name = project["name"] if name is None else name.strip()
    description = project["description"] if description is None else description.strip()
    if (name, description) != (project["name"], project["description"]):
        db.execute("UPDATE projects SET name=?,description=?,updated_at=? WHERE id=?",
                   (name, description, _stamp(), project_id))
    return get_project(db, project_id)


def archive_project(db, project_id: str, *, blockers) -> dict:
    """Require the integrated caller to check all live work under its write locks."""
    _require_write(db)
    project = get_project(db, project_id)
    if project_id == LEGACY_ID:
        raise ProjectConflict("历史兼容项目不能归档")
    if project["archived_at"]:
        return project
    reasons = tuple(blockers(db, project_id))
    if reasons:
        raise ProjectConflict("项目暂不能归档：" + "；".join(reasons))
    stamp = _stamp()
    db.execute("UPDATE projects SET archived_at=?,updated_at=? WHERE id=?", (stamp, stamp, project_id))
    return get_project(db, project_id)


def restore_project(db, project_id: str) -> dict:
    _require_write(db)
    project = get_project(db, project_id)
    if project["archived_at"]:
        db.execute("UPDATE projects SET archived_at='',updated_at=? WHERE id=?", (_stamp(), project_id))
    return get_project(db, project_id)


def batch_ids(db, project_id: str, *, include_deleted: bool = False) -> set[str]:
    get_project(db, project_id)
    predicate = "" if include_deleted else "AND deleted_at=''"
    return {row[0] for row in db.execute(
        f"SELECT id FROM material_batches WHERE project_id=? {predicate}", (project_id,)
    )}


def assert_batch_set(db, project_id: str, ids, *, write: bool = False) -> None:
    if write:
        _require_write(db)
        assert_writable(db, project_id)
    allowed = batch_ids(db, project_id)
    if not set(ids).issubset(allowed):
        raise ProjectConflict("存在不属于当前项目的批次，本次操作未生效")


def resolve_task_project(db, task_id: str) -> dict | None:
    """依据轮次、批次或精确来源路径解析；不能可靠证明时保留为待核验。"""
    if not isinstance(task_id, str) or not task_id:
        return None
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    known: set[str] = set()
    result_columns = ({row[1] for row in db.execute("PRAGMA table_info(capture_run_results)")}
                      if "capture_run_results" in tables else set())
    if "worker_assignments" in tables:
        assignment_columns = {row[1] for row in db.execute("PRAGMA table_info(worker_assignments)")}
        if "project_id" in assignment_columns:
            known.update(row[0] for row in db.execute(
                "SELECT DISTINCT project_id FROM worker_assignments WHERE task_id=? AND project_id!=''",
                (task_id,)))
    if "project_id" in result_columns:
        known.update(row[0] for row in db.execute(
            "SELECT DISTINCT project_id FROM capture_run_results WHERE task_id=? AND project_id!=''",
            (task_id,)))
    batch_columns = ({row[1] for row in db.execute("PRAGMA table_info(material_batches)")}
                     if "material_batches" in tables else set())
    if "batch_id" in result_columns and "project_id" in batch_columns:
        known.update(row[0] for row in db.execute(
            "SELECT DISTINCT b.project_id FROM capture_run_results c "
            "JOIN material_batches b ON b.id=c.batch_id "
            "WHERE c.task_id=? AND c.batch_id!='' AND b.project_id!=''", (task_id,)))
    run_columns = ({row[1] for row in db.execute("PRAGMA table_info(runs)")}
                   if "runs" in tables else set())
    if "project_id" in run_columns and "capture_run_results" in tables:
        known.update(row[0] for row in db.execute(
            "SELECT DISTINCT r.project_id FROM runs r JOIN capture_run_results c ON c.run_id=r.id "
            "WHERE c.task_id=? AND r.project_id!=''", (task_id,)))
    if len(known) > 1:
        return None
    if known:
        try:
            return get_project(db, next(iter(known)))
        except ProjectNotFound:
            return None

    if "tasks" not in tables or "project_id" not in batch_columns:
        return None
    task = db.execute("SELECT source_path FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None or not task[0]:
        return None
    source = str(task[0]).replace("\\", "/").rstrip("/")
    if "material_versions" in tables:
        batch_rows = db.execute(
            "SELECT b.project_id,b.source_root,v.snapshot_id FROM material_batches b "
            "LEFT JOIN material_versions v ON v.batch_id=b.id").fetchall()
    else:
        batch_rows = db.execute(
            "SELECT project_id,source_root,'' FROM material_batches").fetchall()
    matches: set[str] = set()
    for row in batch_rows:
        project_id = str(row[0] or "")
        if not project_id:
            continue
        source_root = str(row[1] or "").replace("\\", "/").rstrip("/")
        snapshot_id = str(row[2] or "")
        if ((source_root and (source == source_root or source.startswith(source_root + "/"))) or
                (snapshot_id and ("/" + snapshot_id + "/") in ("/" + source + "/"))):
            matches.add(project_id)
    if len(matches) != 1:
        return None
    try:
        return get_project(db, next(iter(matches)))
    except ProjectNotFound:
        return None


def assert_task_scope(db, project_id: str, task_id: str, *, write: bool = False) -> dict:
    if write:
        _require_write(db)
    project = resolve_task_project(db, task_id)
    if project is None or project["id"] != project_id:
        raise ProjectNotFound("任务不在当前项目或归属待核验")
    if write:
        assert_writable(db, project_id)
    return project


def selected_version(db, project_id: str) -> str | None:
    get_project(db, project_id)
    row = db.execute("""SELECT s.version_id FROM project_material_selection s
        JOIN material_versions v ON v.id=s.version_id
        JOIN material_batches b ON b.id=v.batch_id
        WHERE s.project_id=? AND b.project_id=s.project_id AND b.deleted_at=''""", (project_id,)).fetchone()
    return row[0] if row else None


def clear_selection(db, project_id: str, batch_id: str) -> bool:
    assert_batch_scope(db, project_id, batch_id, write=True)
    return bool(db.execute("""DELETE FROM project_material_selection WHERE project_id=?
        AND version_id IN (SELECT id FROM material_versions WHERE batch_id=?)""",
        (project_id, batch_id)).rowcount)
