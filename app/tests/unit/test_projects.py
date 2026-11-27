import sqlite3

import pytest

from menu_capture.projects.service import (
    LEGACY_ID, ProjectConflict, ProjectNotFound, assert_batch_scope, create_project,
    migrate_projects, resolve_batch_project, resolve_task_projects, select_version,
)


@pytest.fixture
def db():
    with sqlite3.connect(":memory:") as conn:
        conn.executescript("""
            CREATE TABLE material_batches(id TEXT PRIMARY KEY, deleted_at TEXT, updated_at TEXT);
            CREATE TABLE material_versions(id TEXT PRIMARY KEY, batch_id TEXT);
            CREATE TABLE material_selection(singleton INTEGER PRIMARY KEY, version_id TEXT);
            INSERT INTO material_batches VALUES('b1','','t'),('b2','deleted','t');
            INSERT INTO material_versions VALUES('v1','b1');
            INSERT INTO material_selection VALUES(1,'v1');
        """)
        yield conn


def test_migration_preserves_legacy_and_is_idempotent(db):
    db.execute("BEGIN IMMEDIATE")
    migrate_projects(db)
    assert resolve_batch_project(db, 'b2', include_deleted=True)['id'] == LEGACY_ID
    assert db.execute("SELECT version_id FROM project_material_selection").fetchone() == ('v1',)
    project = create_project(db, '2026-Q4', 'Q4')
    db.execute("INSERT INTO material_batches VALUES('b3','','t',?)", (project['id'],))
    db.execute("INSERT INTO material_versions VALUES('v3','b3')")
    select_version(db, project['id'], 'v3')
    migrate_projects(db)
    assert db.execute("SELECT COUNT(*) FROM project_material_selection").fetchone() == (2,)
    assert resolve_batch_project(db, 'b3')['id'] == project['id']
    assert db.execute("PRAGMA integrity_check").fetchone() == ('ok',)


def test_cross_scope_and_archive_rejected(db):
    db.execute("BEGIN IMMEDIATE")
    migrate_projects(db)
    project = create_project(db, 'Q4', 'Q4')
    with pytest.raises(ProjectNotFound):
        select_version(db, project['id'], 'v1')
    with pytest.raises(ProjectConflict):
        create_project(db, 'q4', 'duplicate')
    for code in ('legacy', 'CON', '../x', 'LPT1', 'a/b'):
        with pytest.raises(ValueError):
            create_project(db, code, 'bad')
    db.execute("UPDATE projects SET archived_at='t' WHERE id=?", (LEGACY_ID,))
    with pytest.raises(ProjectConflict):
        assert_batch_scope(db, LEGACY_ID, 'b1', write=True)
    assert assert_batch_scope(db, LEGACY_ID, 'b1')['id'] == LEGACY_ID
    assert db.execute("SELECT COUNT(*) FROM project_material_selection").fetchone() == (1,)


def test_invalid_old_selection_rolls_back(db):
    db.execute("UPDATE material_selection SET version_id='missing'")
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(ProjectConflict):
        migrate_projects(db)
    db.rollback()
    assert 'project_id' not in {row[1] for row in db.execute('PRAGMA table_info(material_batches)')}
    assert db.execute("SELECT version_id FROM material_selection").fetchone() == ('missing',)


def test_no_silent_backfill_after_migration(db):
    db.execute("BEGIN IMMEDIATE")
    migrate_projects(db)
    db.commit()
    db.execute("INSERT INTO material_batches(id,deleted_at,updated_at) VALUES('bad','','t')")
    with pytest.raises(ProjectConflict):
        migrate_projects(db)
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM material_batches").fetchone() == (2,)


def test_migration_backfills_only_proven_run_history_scope(db):
    db.execute("BEGIN IMMEDIATE")
    migrate_projects(db)
    other = create_project(db, 'other', '其他项目')
    db.execute("INSERT INTO material_batches VALUES('b3','','t',?)", (other['id'],))
    db.execute("CREATE TABLE capture_run_results(run_id TEXT, task_id TEXT, batch_id TEXT, project_id TEXT)")
    db.execute("CREATE TABLE runs(id TEXT, material_batch_id TEXT, project_id TEXT)")
    db.execute("INSERT INTO capture_run_results VALUES('legacy-run','t1','b1','')")
    db.execute("INSERT INTO capture_run_results VALUES('mixed-run','t2','b1','')")
    db.execute("INSERT INTO capture_run_results VALUES('mixed-run','t3','b3','')")
    db.execute("INSERT INTO capture_run_results VALUES('unknown-run','t4','','')")
    db.execute("INSERT INTO runs VALUES('legacy-run','b1','')")
    db.execute("INSERT INTO runs VALUES('mixed-run','','')")
    db.execute("INSERT INTO runs VALUES('unknown-run','','')")

    migrate_projects(db)

    assert db.execute("SELECT project_id FROM capture_run_results WHERE run_id='legacy-run'").fetchone() == (LEGACY_ID,)
    assert db.execute("SELECT project_id FROM runs WHERE id='legacy-run'").fetchone() == (LEGACY_ID,)
    assert db.execute("SELECT project_id FROM capture_run_results WHERE run_id='mixed-run' ORDER BY task_id").fetchall() == [
        (LEGACY_ID,), (other['id'],),
    ]
    assert db.execute("SELECT project_id FROM runs WHERE id='mixed-run'").fetchone() == ('',)
    assert db.execute("SELECT project_id FROM runs WHERE id='unknown-run'").fetchone() == ('',)


def test_project_crud_selection_and_bulk_scope(db):
    from menu_capture.projects import service

    db.execute("BEGIN IMMEDIATE")
    migrate_projects(db)
    p = create_project(db, 'Q4', 'Q4')
    db.execute("INSERT INTO material_batches VALUES('b3','','t',?)", (p['id'],))
    db.execute("INSERT INTO material_versions VALUES('v3','b3')")
    select_version(db, p['id'], 'v3')
    assert service.selected_version(db, LEGACY_ID) == 'v1'
    assert service.selected_version(db, p['id']) == 'v3'
    with pytest.raises(ProjectConflict):
        service.assert_batch_set(db, p['id'], ['b1', 'b3'], write=True)
    service.assert_batch_set(db, p['id'], ['b3'], write=True)
    assert service.clear_selection(db, p['id'], 'b3')
    assert service.selected_version(db, LEGACY_ID) == 'v1'
    changed = service.update_project(db, p['id'], name='Renamed')
    assert changed['code'] == 'Q4'
    with pytest.raises(ProjectConflict):
        service.archive_project(db, p['id'], blockers=lambda *_: ['待补传'])
    assert not service.get_project(db, p['id'])['archived_at']
    archived = service.archive_project(db, p['id'], blockers=lambda *_: [])
    assert archived['archived_at']
    assert service.archive_project(db, p['id'], blockers=lambda *_: ['busy']) == archived
    assert len(service.list_projects(db)) == 1
    assert len(service.list_projects(db, include_archived=True)) == 2
    assert not service.restore_project(db, p['id'])['archived_at']


def test_bulk_task_owners_preserve_mixed_and_legacy_evidence_in_bounded_queries(db):
    db.execute("BEGIN IMMEDIATE")
    migrate_projects(db)
    other = create_project(db, "other", "另一项目")["id"]
    db.execute("ALTER TABLE material_batches ADD COLUMN source_root TEXT NOT NULL DEFAULT ''")
    db.execute("UPDATE material_batches SET source_root='C:/legacy' WHERE id='b1'")
    db.execute("INSERT INTO material_batches VALUES('b3','','t',?,'C:/other')", (other,))
    db.execute("ALTER TABLE material_versions ADD COLUMN snapshot_id TEXT NOT NULL DEFAULT ''")
    db.execute("INSERT INTO material_versions VALUES('v3','b3','other-snapshot')")
    db.execute("CREATE TABLE capture_run_results(run_id TEXT,task_id TEXT,batch_id TEXT,project_id TEXT)")
    db.execute("CREATE TABLE runs(id TEXT,project_id TEXT)")
    db.execute("CREATE TABLE worker_assignments(task_id TEXT,project_id TEXT)")
    db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY, source_path TEXT)")
    db.executemany("INSERT INTO capture_run_results VALUES(?,?,?,?)", [
        ("mixed-run", "legacy", "b1", ""),
        ("mixed-run", "mixed-other", "", other),
        ("mixed-run", "conflict", "b3", LEGACY_ID),
        ("mixed-run", "invalid-project", "", "nonexistent-project"),
        ("other-run", "run-only", "", ""),
    ])
    db.executemany("INSERT INTO runs VALUES(?,?)", [("mixed-run", ""), ("other-run", other)])
    db.execute("INSERT INTO worker_assignments VALUES('assignment-only',?)", (other,))
    db.executemany("INSERT INTO tasks VALUES(?,?)", [
        ("legacy-root", r"C:\legacy\store.png"),
        ("prefix-is-not-root", "C:/legacy-old/store.png"),
        ("snapshot", "C:/published/other-snapshot/store.png"),
        ("partial-snapshot", "C:/published/other-snapshot-old/store.png"),
        ("conflict", "C:/legacy/store.png"),
        ("invalid-project", "C:/legacy/store.png"),
    ])
    expected = {
        "legacy": LEGACY_ID, "mixed-other": other, "conflict": None, "invalid-project": None,
        "run-only": other, "assignment-only": other, "legacy-root": LEGACY_ID,
        "prefix-is-not-root": None, "snapshot": other, "partial-snapshot": None,
    }
    missing = [f"missing-{n}" for n in range(505)]
    statements = []
    db.set_trace_callback(statements.append)
    try:
        resolved = resolve_task_projects(db, [*missing, *expected, "legacy", ""])
    finally:
        db.set_trace_callback(None)
    assert {key: resolved[key]["id"] if resolved[key] else None for key in expected} == expected
    assert all(resolved[key] is None for key in missing)
    assert "" not in resolved and len(resolved) == len(expected) + len(missing)
    assert len(statements) <= 20
