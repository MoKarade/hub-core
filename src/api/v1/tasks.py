"""Endpoint /v1/tasks - ingest Google Tasks API (Phase 5)."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Task as TaskModel
from src.db.session import get_db
from src.services.oauth_google import get_valid_access_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tasks", tags=["tasks"])

TASKS_API = "https://tasks.googleapis.com/tasks/v1"


class TasksSyncRequest(BaseModel):
    user_email: str = Field(default="marc.richard4@gmail.com")
    show_completed: bool = True


class TasksSyncResponse(BaseModel):
    tasklists_synced: int
    tasks_ingested: int
    tasks_updated: int
    errors: int
    duration_seconds: float


class TaskItem(BaseModel):
    id: UUID
    task_id: str
    tasklist_id: str
    tasklist_title: str | None
    title: str | None
    notes: str | None
    is_completed: bool
    due_at: datetime | None
    completed_at: datetime | None


async def _resolve_token(db: AsyncSession, user_email: str) -> str:
    for service in ("tasks", "all"):
        try:
            return await get_valid_access_token(db, service=service, user_email=user_email)
        except RuntimeError:
            continue
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Pas de token Tasks pour {user_email}")


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


@router.post("/sync", response_model=TasksSyncResponse)
async def sync_tasks(
    payload: TasksSyncRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TasksSyncResponse:
    start = time.monotonic()
    access_token = await _resolve_token(db, payload.user_email)

    ingested = 0
    updated = 0
    errors = 0
    tasklists_count = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(
                f"{TASKS_API}/users/@me/lists",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"maxResults": 100},
            )
            r.raise_for_status()
            tasklists = r.json().get("items", [])
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Tasklists failed: {e.response.status_code}",
            ) from e

        for tl in tasklists:
            tl_id = tl.get("id")
            tl_title = tl.get("title")
            if not tl_id:
                continue
            tasklists_count += 1

            page_token: str | None = None
            while True:
                params: dict[str, Any] = {
                    "maxResults": 100,
                    "showCompleted": str(payload.show_completed).lower(),
                    "showHidden": "true",
                }
                if page_token:
                    params["pageToken"] = page_token
                try:
                    r = await client.get(
                        f"{TASKS_API}/lists/{tl_id}/tasks",
                        headers={"Authorization": f"Bearer {access_token}"},
                        params=params,
                    )
                    r.raise_for_status()
                    data = r.json()
                except httpx.HTTPStatusError as e:
                    logger.warning(
                        "tasks_list_failed: tl=%s status=%d", tl_id, e.response.status_code
                    )
                    errors += 1
                    break

                for t in data.get("items", []):
                    parsed = {
                        "task_id": t["id"],
                        "tasklist_id": tl_id,
                        "tasklist_title": tl_title,
                        "title": t.get("title"),
                        "notes": t.get("notes"),
                        "is_completed": t.get("status") == "completed",
                        "due_at": _parse_dt(t.get("due")),
                        "completed_at": _parse_dt(t.get("completed")),
                        "last_modified": _parse_dt(t.get("updated")),
                    }
                    existing = (
                        await db.execute(
                            select(TaskModel).where(TaskModel.task_id == parsed["task_id"])
                        )
                    ).scalar_one_or_none()
                    if existing:
                        for k, v in parsed.items():
                            setattr(existing, k, v)
                        updated += 1
                    else:
                        db.add(TaskModel(user_email=payload.user_email, **parsed))
                        ingested += 1

                page_token = data.get("nextPageToken")
                if not page_token:
                    break

        await db.commit()

    return TasksSyncResponse(
        tasklists_synced=tasklists_count,
        tasks_ingested=ingested,
        tasks_updated=updated,
        errors=errors,
        duration_seconds=round(time.monotonic() - start, 2),
    )


class TaskToggleRequest(BaseModel):
    user_email: str = Field(default="marc.richard4@gmail.com")
    completed: bool


@router.post("/{task_id}/toggle", response_model=TaskItem)
async def toggle_task(
    task_id: str,
    payload: TaskToggleRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskItem:
    """Toggle complete/incomplete d'une tache. Sync avec Google Tasks API."""
    access_token = await _resolve_token(db, payload.user_email)

    # Recupere la tache locale pour avoir le tasklist_id
    local = (
        await db.execute(select(TaskModel).where(TaskModel.task_id == task_id))
    ).scalar_one_or_none()
    if not local:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tache introuvable")

    # PATCH Google Tasks
    body = {"status": "completed" if payload.completed else "needsAction"}
    if not payload.completed:
        body["completed"] = None  # type: ignore

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.patch(
                f"{TASKS_API}/lists/{local.tasklist_id}/tasks/{task_id}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            r.raise_for_status()
            updated = r.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Google Tasks toggle failed: {e.response.status_code} {e.response.text[:200]}",
            ) from e

    # Update DB
    local.is_completed = payload.completed
    local.completed_at = _parse_dt(updated.get("completed"))
    local.last_modified = _parse_dt(updated.get("updated"))
    await db.commit()

    return TaskItem(
        id=local.id,
        task_id=local.task_id,
        tasklist_id=local.tasklist_id,
        tasklist_title=local.tasklist_title,
        title=local.title,
        notes=local.notes,
        is_completed=local.is_completed,
        due_at=local.due_at,
        completed_at=local.completed_at,
    )


class TaskCreateRequest(BaseModel):
    user_email: str = Field(default="marc.richard4@gmail.com")
    tasklist_id: str
    title: str = Field(..., min_length=1, max_length=500)
    notes: str | None = None
    due_at: datetime | None = None


@router.post("/create", response_model=TaskItem)
async def create_task(
    payload: TaskCreateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskItem:
    """Cree une nouvelle tache. Sync avec Google Tasks API."""
    access_token = await _resolve_token(db, payload.user_email)
    body: dict[str, Any] = {"title": payload.title}
    if payload.notes:
        body["notes"] = payload.notes
    if payload.due_at:
        body["due"] = payload.due_at.isoformat().replace("+00:00", "Z")

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.post(
                f"{TASKS_API}/lists/{payload.tasklist_id}/tasks",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            r.raise_for_status()
            created = r.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Google Tasks create failed: {e.response.status_code}",
            ) from e

    # Recupere le titre de la tasklist
    tl_title = (
        await db.execute(
            select(TaskModel.tasklist_title)
            .where(TaskModel.tasklist_id == payload.tasklist_id)
            .limit(1)
        )
    ).scalar_one_or_none()

    new_local = TaskModel(
        user_email=payload.user_email,
        task_id=created["id"],
        tasklist_id=payload.tasklist_id,
        tasklist_title=tl_title,
        title=created.get("title"),
        notes=created.get("notes"),
        is_completed=created.get("status") == "completed",
        due_at=_parse_dt(created.get("due")),
        completed_at=_parse_dt(created.get("completed")),
        last_modified=_parse_dt(created.get("updated")),
    )
    db.add(new_local)
    await db.commit()
    await db.refresh(new_local)

    return TaskItem(
        id=new_local.id,
        task_id=new_local.task_id,
        tasklist_id=new_local.tasklist_id,
        tasklist_title=new_local.tasklist_title,
        title=new_local.title,
        notes=new_local.notes,
        is_completed=new_local.is_completed,
        due_at=new_local.due_at,
        completed_at=new_local.completed_at,
    )


@router.delete("/{task_id}")
async def delete_task(
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user_email: Annotated[str, Query()] = "marc.richard4@gmail.com",
) -> dict[str, str]:
    """Supprime une tache. Sync avec Google Tasks API."""
    access_token = await _resolve_token(db, user_email)
    local = (
        await db.execute(select(TaskModel).where(TaskModel.task_id == task_id))
    ).scalar_one_or_none()
    if not local:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tache introuvable")

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.delete(
                f"{TASKS_API}/lists/{local.tasklist_id}/tasks/{task_id}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Google Tasks delete failed: {e.response.status_code}",
            ) from e

    await db.delete(local)
    await db.commit()
    return {"deleted": task_id}


class TasklistInfo(BaseModel):
    id: str
    title: str | None
    count: int


@router.get("/lists", response_model=list[TasklistInfo])
async def list_tasklists(db: Annotated[AsyncSession, Depends(get_db)]) -> list[TasklistInfo]:
    """Liste les tasklists disponibles (depuis DB locale)."""
    rows = (
        await db.execute(
            select(
                TaskModel.tasklist_id,
                TaskModel.tasklist_title,
                func.count(TaskModel.id).label("count"),
            ).group_by(TaskModel.tasklist_id, TaskModel.tasklist_title)
        )
    ).all()
    return [TasklistInfo(id=r[0], title=r[1], count=int(r[2])) for r in rows]


@router.get("", response_model=list[TaskItem])
async def list_tasks(
    db: Annotated[AsyncSession, Depends(get_db)],
    completed: Annotated[bool | None, Query()] = None,
    tasklist_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[TaskItem]:
    stmt = select(TaskModel).order_by(
        desc(TaskModel.due_at).nullslast(), desc(TaskModel.last_modified)
    )
    if completed is not None:
        stmt = stmt.where(TaskModel.is_completed == completed)
    if tasklist_id:
        stmt = stmt.where(TaskModel.tasklist_id == tasklist_id)
    stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        TaskItem(
            id=t.id,
            task_id=t.task_id,
            tasklist_id=t.tasklist_id,
            tasklist_title=t.tasklist_title,
            title=t.title,
            notes=t.notes,
            is_completed=t.is_completed,
            due_at=t.due_at,
            completed_at=t.completed_at,
        )
        for t in rows
    ]


class TasksStats(BaseModel):
    total: int
    pending: int
    completed: int
    overdue: int
    by_tasklist: list[dict[str, Any]]


@router.get("/stats", response_model=TasksStats)
async def tasks_stats(db: Annotated[AsyncSession, Depends(get_db)]) -> TasksStats:
    from datetime import UTC
    from datetime import datetime as dtm

    total = (await db.execute(select(func.count(TaskModel.id)))).scalar() or 0
    pending = (
        await db.execute(select(func.count(TaskModel.id)).where(TaskModel.is_completed.is_(False)))
    ).scalar() or 0
    completed = (
        await db.execute(select(func.count(TaskModel.id)).where(TaskModel.is_completed.is_(True)))
    ).scalar() or 0
    overdue = (
        await db.execute(
            select(func.count(TaskModel.id)).where(
                TaskModel.is_completed.is_(False),
                TaskModel.due_at.isnot(None),
                TaskModel.due_at < dtm.now(UTC),
            )
        )
    ).scalar() or 0
    by_q = (
        select(
            TaskModel.tasklist_title,
            func.count(TaskModel.id).label("count"),
        )
        .group_by(TaskModel.tasklist_title)
        .order_by(desc("count"))
    )
    by_list = [
        {"tasklist": r[0] or "(sans nom)", "count": int(r[1])}
        for r in (await db.execute(by_q)).all()
    ]
    return TasksStats(
        total=total,
        pending=pending,
        completed=completed,
        overdue=overdue,
        by_tasklist=by_list,
    )
