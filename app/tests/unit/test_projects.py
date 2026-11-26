import sqlite3

import pytest

from menu_capture.projects.service import (
    LEGACY_ID, ProjectConflict, ProjectNotFound, assert_batch_scope, create_project,
    migrate_projects, resolve_batch_project, select_version,
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
