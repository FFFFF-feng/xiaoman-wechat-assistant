"""调度器只入队；Gateway 服务经官方渠道适配器发送，再回写平台受理状态。"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .clawbot_identity import clawbot_owner, is_clawbot_owner
from .models import ClawbotDelivery, Reminder, Task
from .task_dialogue import remember_send
from .assistant_preferences import snapshot
from .reply_style import reminder_reply
from . import reminder_followups

CLAIM_LIFETIME = timedelta(seconds=45)
SEND_UNCERTAIN_AFTER = timedelta(seconds=120)
_worker_seen_at = 0.0


def enabled() -> bool:
    return os.getenv("CLAWBOT_ENABLED", "false").lower() == "true" and os.getenv("CLAWBOT_REMINDERS_ENABLED", "false").lower() == "true"


def local_now() -> datetime:
    return datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Asia/Shanghai"))).replace(tzinfo=None)


def max_lateness() -> timedelta:
    return timedelta(seconds=max(1, int(os.getenv("CLAWBOT_REMINDER_MAX_LATENESS_SECONDS", "300"))))


def status() -> dict:
    return {"configured": enabled(), "worker_online": enabled() and time.monotonic() - _worker_seen_at < 20}


def enqueue(db: Session, reminder: Reminder, owner: str, title: str) -> None:
    if not is_clawbot_owner(owner):
        raise ValueError("invalid clawbot owner")
    if db.scalar(select(ClawbotDelivery).where(ClawbotDelivery.reminder_id == reminder.id)):
        return
    reminder.channel = "clawbot"
    reminder.status = "queued"
    db.add(ClawbotDelivery(reminder_id=reminder.id, owner_id=owner,
        content=reminder_followups.stage_text(db, reminder, title,
            reminder_text(db, owner, title, reminder.reminder_time), local_now())))


def reminder_text(db: Session, owner: str, title: str, due_at: datetime) -> str:
    return reminder_reply(title, snapshot(db, owner), owner + '|' + due_at.isoformat() + '|' + title)


def _valid_task(db: Session, item: ClawbotDelivery, now: datetime) -> bool:
    reminder = db.get(Reminder, item.reminder_id)
    task = db.get(Task, reminder.task_id) if reminder else None
    reason = None
    if not task or task.owner_id != item.owner_id or task.status not in {"open", "snoozed"} or not task.reminder_enabled:
        reason = "cancelled"
    elif task.due_at != reminder_followups.task_due(db, reminder):
        reason = "superseded"
    elif (stage_reason := reminder_followups.valid(db, reminder, task, now)) is not None:
        reason = stage_reason
    elif reminder.reminder_time > now:
        reason = 'not_due'
    elif now - reminder.reminder_time > max_lateness():
        reason = "missed"
    if reason:
        item.status = reason
        item.error_code = reason
        item.finished_at = now
        if reminder:
            reminder.status = reason
            reminder.error_code = reason
        return False
    return True


def recover(db: Session, now: datetime) -> None:
    for item in db.scalars(select(ClawbotDelivery).where(ClawbotDelivery.status.in_(["claimed", "sending"]))):
        if item.status == "claimed" and item.claimed_at and now - item.claimed_at > CLAIM_LIFETIME:
            # 未开始发送的过期领取可恢复；开始发送后则不能假设未到达平台。
            item.status = "pending"
            item.claim_hash = None
        elif item.status == "sending" and item.started_at and now - item.started_at > SEND_UNCERTAIN_AFTER:
            item.status = "unknown"
            item.error_code = "send_outcome_unknown"
            reminder = db.get(Reminder, item.reminder_id)
            reminder.status = "unknown"
            reminder.error_code = item.error_code
            reminder_followups.result(db, item, 'unknown')
    db.commit()


def claim(db: Session, bindings: list[dict[str, str]]) -> dict | None:
    global _worker_seen_at
    if not enabled():
        raise HTTPException(503, "主动提醒未启用")
    _worker_seen_at = time.monotonic()
    now = local_now()
    recover(db, now)
    mapping = {clawbot_owner(v["account_id"], v["sender_id"]): v for v in bindings}
    items = db.scalars(select(ClawbotDelivery).where(ClawbotDelivery.status == "pending").order_by(ClawbotDelivery.id).limit(20)).all()
    for item in items:
        if not _valid_task(db, item, now):
            db.commit()
            continue
        binding = mapping.get(item.owner_id)
        if not binding:
            item.status = "blocked"
            item.error_code = "binding_removed"
            reminder = db.get(Reminder, item.reminder_id)
            reminder.status = "blocked"
            reminder.error_code = item.error_code
            db.commit()
            continue
        token = secrets.token_hex(32)
        reminder = db.get(Reminder, item.reminder_id)
        task = db.get(Task, reminder.task_id)
        content = reminder_followups.stage_text(db, reminder, task.title,
            reminder_text(db, item.owner_id, task.title, reminder.reminder_time), now)
        changed = db.execute(update(ClawbotDelivery).where(ClawbotDelivery.id == item.id,
            ClawbotDelivery.status == "pending").values(status="claimed", claimed_at=now,
            claim_hash=hashlib.sha256(token.encode()).hexdigest(), content=content))
        db.commit()
        if changed.rowcount == 1:
            return {"id": item.id, "claim_token": token, "account_id": binding["account_id"],
                    "sender_id": binding["sender_id"], "text": content}
    return None


def leased_item(db: Session, delivery_id: int, token: str) -> ClawbotDelivery:
    item = db.get(ClawbotDelivery, delivery_id)
    digest = hashlib.sha256(token.encode()).hexdigest()
    if not item or not item.claim_hash or not hmac.compare_digest(item.claim_hash, digest):
        raise HTTPException(409, "发送领取凭据无效")
    return item


def begin_send(db: Session, delivery_id: int, token: str, bindings: list[dict[str, str]]) -> None:
    if not enabled():
        raise HTTPException(503, "主动提醒未启用")
    item = leased_item(db, delivery_id, token)
    now = local_now()
    if item.status != "claimed" or not item.claimed_at or now - item.claimed_at > CLAIM_LIFETIME:
        raise HTTPException(409, "发送领取已失效")
    owners = {clawbot_owner(v["account_id"], v["sender_id"]) for v in bindings}
    if item.owner_id not in owners:
        raise HTTPException(409, "绑定已撤销")
    if not _valid_task(db, item, now):
        db.commit()
        raise HTTPException(409, "任务已经结束、改期或过期，不发送提醒")
    changed = db.execute(update(ClawbotDelivery).where(ClawbotDelivery.id == item.id,
        ClawbotDelivery.status == "claimed", ClawbotDelivery.claim_hash == item.claim_hash
    ).values(status="sending", started_at=now))
    if changed.rowcount == 1:
        remember_send(db, item, now)
    db.commit()
    if changed.rowcount != 1:
        raise HTTPException(409, "发送已经开始")


def complete_send(db: Session, delivery_id: int, token: str, outcome: str, message_id: str | None, error_code: str | None) -> None:
    item = leased_item(db, delivery_id, token)
    if item.status == outcome:
        return  # ACK 可重复，不重新发送。
    if item.status not in {"sending", "unknown"}:
        raise HTTPException(409, "发送状态不允许更新")
    if outcome == "accepted" and not message_id:
        raise HTTPException(422, "平台受理须有消息标识")
    item.status = outcome
    item.finished_at = local_now()
    item.external_message_id = message_id
    item.error_code = error_code
    reminder = db.get(Reminder, item.reminder_id)
    reminder.status = outcome
    reminder.external_message_id = message_id
    reminder.error_code = error_code
    if outcome == "accepted":
        reminder.sent_at = item.finished_at
    reminder_followups.result(db, item, outcome)
    db.commit()
