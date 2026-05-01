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
