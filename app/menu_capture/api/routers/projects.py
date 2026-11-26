"""Hub project API. Register only after the project-aware integration is ready."""
from contextlib import closing
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


def register(app: FastAPI, *, store, role: str, archive_blockers: Callable) -> None:
    """Hub auth/CSRF come from create_app; no worker mutation endpoints are exposed.

    The integrated caller must supply real archive blockers (including in-memory
    jobs and pending uploads), and coordinate job admission with archive writes.
    """
    if role != "hub":
        raise ValueError("项目管理路由只能在共享端注册")

    def execute(operation, *args, write=False, **kwargs):
        try:
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
        return execute(service.list_projects, include_archived=include_archived)

    @app.post("/api/projects", status_code=201)
    def create(payload: CreateProject):
        return execute(service.create_project, **payload.model_dump(), write=True)

    @app.get("/api/projects/{project_id}")
    def detail(project_id: str):
        return execute(service.get_project, project_id)

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
