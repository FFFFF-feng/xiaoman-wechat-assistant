from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .models import Task
from .schedule_access import schedule_owner
from .schemas import SnoozeRequest, TaskRead
from .wechat_notifications import NotificationBlocked, plan_reminder

router = APIRouter(prefix="/api/schedule", tags=["微信个人日程"])
LOCAL_TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Shanghai"))


def _owned_task(db: Session, task_id: int, owner_id: str) -> Task:
    task = db.scalar(select(Task).where(Task.id == task_id, Task.owner_id == owner_id))
    if not task:
        raise HTTPException(status_code=404, detail="日程不存在")
    return task


def _local_naive(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(LOCAL_TZ).replace(tzinfo=None)
    return value


@router.get("/tasks", response_model=list[TaskRead])
def list_own_schedule(
    response: Response,
    start: date = Query(...),
    end: date = Query(...),
    owner_id: str = Depends(schedule_owner),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    if end < start or (end - start).days > 30:
        raise HTTPException(status_code=422, detail="日期范围须在 1 至 31 天内")
    start_at = datetime.combine(start, time.min)
    end_at = datetime.combine(end + timedelta(days=1), time.min)
    return db.scalars(
        select(Task)
        .where(Task.owner_id == owner_id, Task.due_at >= start_at, Task.due_at < end_at)
        .order_by(Task.due_at.asc(), Task.id.asc())
    ).all()


@router.post("/tasks/{task_id}/complete", response_model=TaskRead)
def complete_own_task(
    task_id: int,
    response: Response,
    owner_id: str = Depends(schedule_owner),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    task = _owned_task(db, task_id, owner_id)
    if task.status in {"done", "cancelled"}:
        raise HTTPException(status_code=409, detail="这条日程已经结束")
    task.status = "done"
    task.updated_at = datetime.now(LOCAL_TZ).replace(tzinfo=None)
    db.commit()
    db.refresh(task)
    return task


@router.post("/tasks/{task_id}/snooze", response_model=TaskRead)
def snooze_own_task(
    task_id: int,
    payload: SnoozeRequest,
    response: Response,
    owner_id: str = Depends(schedule_owner),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    task = _owned_task(db, task_id, owner_id)
    if task.status in {"done", "cancelled"}:
        raise HTTPException(status_code=409, detail="这条日程已经结束")
    due_at = _local_naive(payload.due_at)
    if due_at <= datetime.now(LOCAL_TZ).replace(tzinfo=None):
        raise HTTPException(status_code=422, detail="请选择将来的提醒时间")
    try:
        channel, template_id = plan_reminder(db, owner_id, due_at, task.title,
                                             exclude_task_id=task.id, reserve_reply=False)
    except NotificationBlocked as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    task.due_at = due_at
    task.reminder_channel = channel
    task.notification_template_id = template_id
    task.status = "snoozed"
    task.updated_at = datetime.now(LOCAL_TZ).replace(tzinfo=None)
    db.commit()
    db.refresh(task)
    return task


@router.post("/tasks/{task_id}/cancel", response_model=TaskRead)
def cancel_own_task(
    task_id: int,
    response: Response,
    owner_id: str = Depends(schedule_owner),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    task = _owned_task(db, task_id, owner_id)
    if task.status in {"done", "cancelled"}:
        raise HTTPException(status_code=409, detail="这条日程已经结束")
    task.status = "cancelled"
    task.updated_at = datetime.now(LOCAL_TZ).replace(tzinfo=None)
    db.commit()
    db.refresh(task)
    return task
