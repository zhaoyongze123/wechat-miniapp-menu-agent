import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from menu_capture.api.routers.projects import register
from menu_capture.projects.service import migrate_projects


class Store:
    def __init__(self, path):
        self.path = path

    def connect(self):
        return sqlite3.connect(self.path)


def test_project_routes(tmp_path):
    store = Store(tmp_path / 'test.sqlite')
    with store.connect() as db:
        db.executescript('''CREATE TABLE material_batches(id TEXT PRIMARY KEY, deleted_at TEXT, updated_at TEXT);
            CREATE TABLE material_versions(id TEXT PRIMARY KEY, batch_id TEXT);
            CREATE TABLE material_selection(singleton INTEGER PRIMARY KEY, version_id TEXT);''')
        db.execute('BEGIN IMMEDIATE')
        migrate_projects(db)
    app = FastAPI()
    blockers = ['待补传']
    register(app, store=store, role='hub', archive_blockers=lambda *_: blockers)
    client = TestClient(app)
    assert client.get('/api/projects').json() == []
    payload = {'code': 'Q4', 'name': 'Q4'}
    response = client.post('/api/projects', json=payload)
    assert response.status_code == 201
    project = response.json()
    url = '/api/projects/' + project['id']
    assert client.post('/api/projects', json=payload).status_code == 409
    assert client.get('/api/projects/unknown').status_code == 404
    assert client.patch(url, json={'code': 'other'}).status_code == 422
    assert client.patch(url, json={'name': None}).status_code == 400
    assert client.patch(url, json={'name': 'New'}).json()['code'] == 'Q4'
    assert client.post(url + '/archive').status_code == 409
    blockers.clear()
    assert client.post(url + '/archive').json()['archived_at']
    assert client.get('/api/projects').json() == []
    assert len(client.get('/api/projects?include_archived=true').json()) == 1
    assert not client.post(url + '/restore').json()['archived_at']
    with pytest.raises(ValueError):
        register(FastAPI(), store=store, role='worker', archive_blockers=lambda *_: [])
