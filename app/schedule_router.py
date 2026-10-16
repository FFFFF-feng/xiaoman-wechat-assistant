from __future__ import annotations

import os
import hashlib
import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from .database import get_db
from .models import ScheduleCreation, Task
from .schedule_access import schedule_owner
from .schemas import ScheduleTaskCreate, ScheduleTaskEdit, SnoozeRequest, TaskRead
from .wechat_notifications import NotificationBlocked, app_id, plan_reminder

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


def _plan_fields(db: Session, owner: str, payload, exclude_task_id: int | None = None):
    due = _local_naive(payload.due_at)
    if not payload.reminder_enabled:
        return due, None, None
    if due <= datetime.now(LOCAL_TZ).replace(tzinfo=None):
        raise HTTPException(422, "请选择将来的提醒时间；补记过去的事可以关闭微信提醒")
    try:
        channel, template = plan_reminder(db, owner, due, payload.title,
                                          exclude_task_id=exclude_task_id, reserve_reply=False)
    except NotificationBlocked as exc:
        raise HTTPException(422, str(exc)) from None
    return due, channel, template


@router.post("/tasks", response_model=TaskRead)
def create_own_task(payload: ScheduleTaskCreate, response: Response,
                    owner_id: str = Depends(schedule_owner), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    key = (owner_id, str(payload.request_id))
    data = payload.model_dump(mode="json", exclude={"request_id"})
    data["due_at"] = _local_naive(payload.due_at).isoformat()
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def cached(receipt):
        if receipt.fingerprint != fingerprint:
            raise HTTPException(409, "这次保存的内容已变化，请先刷新日程核对，不要重复创建")
        return TaskRead.model_validate_json(receipt.response_json)

    if receipt := db.get(ScheduleCreation, key):
        return cached(receipt)
    due, channel, template = _plan_fields(db, owner_id, payload)
    task = Task(**{**data, "due_at": due}, owner_id=owner_id, reminder_channel=channel,
                notification_template_id=template,
                wechat_app_id=(app_id() or None) if owner_id.startswith("wechat:") else None)
    db.add(task)
    try:
        db.flush()
        result = TaskRead.model_validate(task)
        db.add(ScheduleCreation(owner_id=owner_id, request_id=key[1], fingerprint=fingerprint,
                                response_json=result.model_dump_json()))
        db.commit()
        return result
    except IntegrityError:
        db.rollback()
        if receipt := db.get(ScheduleCreation, key):
            return cached(receipt)
        raise HTTPException(409, "正在保存，请刷新日程核对结果") from None


@router.post("/tasks/{task_id}/edit", response_model=TaskRead)
def edit_own_task(task_id: int, payload: ScheduleTaskEdit, response: Response,
                  owner_id: str = Depends(schedule_owner), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    task = _owned_task(db, task_id, owner_id)
    if task.status not in {"open", "snoozed"}:
        raise HTTPException(409, "这条日程已经结束，不能继续编辑")
    if task.updated_at != _local_naive(payload.expected_updated_at):
        raise HTTPException(409, "这条日程刚在别处修改过，请刷新后再编辑")
    due, channel, template = _plan_fields(db, owner_id, payload, task.id)
    values = payload.model_dump(exclude={"expected_updated_at"})
    values.update(due_at=due, reminder_channel=channel, notification_template_id=template,
                  status="snoozed" if task.due_at != due else task.status,
                  updated_at=datetime.now(LOCAL_TZ).replace(tzinfo=None))
    changed = db.execute(update(Task).where(Task.id == task_id, Task.owner_id == owner_id,
        Task.updated_at == _local_naive(payload.expected_updated_at), Task.status.in_(["open", "snoozed"])).values(**values))
    if changed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "这条日程刚在别处修改过，请刷新后再编辑")
    db.commit(); db.refresh(task)
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
        channel, template_id = (plan_reminder(db, owner_id, due_at, task.title,
                                             exclude_task_id=task.id, reserve_reply=False)
                                if task.reminder_enabled else (None, None))
    except NotificationBlocked as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    task.due_at = due_at
    task.reminder_channel = channel
    task.notification_template_id = template_id
    if not task.reminder_enabled:
        task.reminder_channel = task.notification_template_id = None
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
