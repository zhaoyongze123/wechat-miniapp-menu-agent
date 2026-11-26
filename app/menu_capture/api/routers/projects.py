"""Hub project API. Register only after the project-aware integration is ready."""
from contextlib import closing, nullcontext
from typing import Callable

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from menu_capture.projects import service


class CreateProject(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str
    name: str
    description: str = ""


class UpdateProject(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str | None = None
    description: str | None = None


def register(app: FastAPI, *, store, role: str, archive_blockers: Callable,
             control_lock, worker=None) -> None:
    """Expose Hub project management and only locally assigned projects on Workers."""

    def execute(operation, *args, write=False, **kwargs):
        try:
            lock = control_lock if write else nullcontext()
            with lock:
                with closing(store.connect()) as db, db:
                    if write:
                        db.execute("BEGIN IMMEDIATE")
                    return operation(db, *args, **kwargs)
        except service.ProjectNotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except service.ProjectConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/projects")
    def projects(include_archived: bool = False):
        if role == "worker":
            return [row for row in (worker.projects() if worker else [])
                    if include_archived or not row.get("archived_at")]
        return execute(service.list_projects, include_archived=include_archived)

    @app.get("/api/projects/{project_id}")
    def detail(project_id: str):
        if role == "worker":
            row = next((item for item in (worker.projects() if worker else []) if item["id"] == project_id), None)
            if row is None:
                raise HTTPException(404, "项目不存在或该采集端没有分配记录")
            return row
        return execute(service.get_project, project_id)

    if role != "hub":
        return

    @app.post("/api/projects", status_code=201)
    def create(payload: CreateProject):
        return execute(service.create_project, **payload.model_dump(), write=True)

    @app.patch("/api/projects/{project_id}")
    def edit(project_id: str, payload: UpdateProject):
        if any(value is None for value in payload.model_dump(exclude_unset=True).values()):
            raise HTTPException(400, "项目名称及描述不能为 null")
        return execute(service.update_project, project_id, **payload.model_dump(exclude_unset=True), write=True)

    @app.post("/api/projects/{project_id}/archive")
    def archive(project_id: str):
        return execute(service.archive_project, project_id, blockers=archive_blockers, write=True)

    @app.post("/api/projects/{project_id}/restore")
    def restore(project_id: str):
        return execute(service.restore_project, project_id, write=True)
