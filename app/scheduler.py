from __future__ import annotations

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from .adapters.mock import MockChatAdapter
from .adapters.wechat_official import WechatAPIError
from .database import SessionLocal
from .models import Message, Reminder, Task
from .clawbot_identity import is_clawbot_owner
from . import clawbot_delivery
from . import clawbot_stickers
from .clawbot_bindings import load_bindings
from .wechat_notifications import NotificationBlocked, app_id, apply_receipt, reminder_mode, send_reminder

logger = logging.getLogger(__name__)
CHECK_SECONDS = int(os.getenv("REMINDER_CHECK_SECONDS", "5"))
MAX_WECHAT_LATENESS_SECONDS = int(os.getenv("WECHAT_REMINDER_MAX_LATENESS_SECONDS", "3600"))
LOCAL_TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Shanghai"))
adapter = MockChatAdapter()
scheduler = BackgroundScheduler()


def _send_due_reminder(task: Task, content: str, db, now: datetime) -> tuple[str, dict]:
    """公众号按身份投递；ClawBot 未接投递器时阻断，保留其他渠道既有行为。"""
    if task.owner_id and task.owner_id.startswith("wechat:"):
        return send_reminder(db, task, content, now)
    if is_clawbot_owner(task.owner_id):
        raise NotificationBlocked("微信插件主动提醒投递器尚未接通", "clawbot_not_configured")
    adapter.send("local-user", content)
    return "mock", {"errcode": 0}


def check_due_tasks() -> None:
    # API 允许传入带秒的时间，检查时保留秒以便本地测试和短提醒及时触发。
    now = datetime.now(LOCAL_TZ).replace(tzinfo=None)
    db = SessionLocal()
    try:
        tasks = db.scalars(
            select(Task).where(Task.status.in_(["open", "snoozed"]), Task.due_at <= now, Task.reminder_enabled.is_(True))
        ).all()
        for task in tasks:
            db.refresh(task)
            if task.status not in {"open", "snoozed"} or task.due_at > now or not task.reminder_enabled:
                continue
            existing = db.scalar(
                select(Reminder).where(
                    Reminder.task_id == task.id,
                    Reminder.reminder_time == task.due_at,
                )
            )
            if existing:
                continue

            # 先落库占位，再调用微信。进程中断时不自动重发不确定的请求，避免重复提醒。
            reminder = Reminder(task_id=task.id, reminder_time=task.due_at, status="sending")
            db.add(reminder)
            content = f"该完成任务啦：{task.title}"
            if task.owner_id is None:
                db.add(Message(message_type="reminder", content=content, created_at=now))
            db.commit()
            if is_clawbot_owner(task.owner_id) and clawbot_delivery.enabled():
                if now - task.due_at > clawbot_delivery.max_lateness():
                    reminder.channel = "clawbot"
                    reminder.status = "missed"
                    reminder.error_code = "too_late"
                else:
                    clawbot_delivery.enqueue(db, reminder, task.owner_id, task.title)
                db.commit()
                continue
            if (
                task.owner_id
                and task.owner_id.startswith("wechat:")
                and (now - task.due_at).total_seconds() > MAX_WECHAT_LATENESS_SECONDS
            ):
                reminder.status = "missed"
                reminder.error_code = "too_late"
                reminder.error = "服务离线过久，超过允许的补发时间"
                db.commit()
                logger.warning("跳过已过期的微信提醒 task_id=%s due_at=%s", task.id, task.due_at)
                continue
            try:
                reminder.channel = (task.reminder_channel or reminder_mode()) if task.owner_id and task.owner_id.startswith("wechat:") else "mock"
                if is_clawbot_owner(task.owner_id):
                    reminder.channel = "clawbot_pending"
                channel, result = _send_due_reminder(task, content, db, now)
            except Exception as exc:
                logger.exception("发送到期提醒失败 task_id=%s channel=%s", task.id,
                                 "wechat" if task.owner_id and task.owner_id.startswith("wechat:") else "local")
                reminder.status = "blocked" if isinstance(exc, NotificationBlocked) or (
                    isinstance(exc, WechatAPIError) and exc.code in {"45015", "45047", "48001", "43101", "40003"}) else "failed"
                reminder.error_code = str(getattr(exc, "code", type(exc).__name__))
                reminder.error = str(exc)[:300]
            else:
                reminder.channel = channel
                reminder.external_message_id = str(result.get("msgid", result.get("msg_id", ""))) or None
                reminder.status = "sent" if channel == "mock" else "accepted"
                if channel == "mock":
                    reminder.sent_at = datetime.now(LOCAL_TZ).replace(tzinfo=None)
                apply_receipt(db, reminder, task.wechat_app_id or app_id())
            db.commit()
    except Exception:
        db.rollback()
        logger.exception("检查到期任务失败")
    finally:
        db.close()


def check_daily_stickers() -> None:
    if not clawbot_stickers.enabled():
        return
    db = SessionLocal()
    try:
        clawbot_stickers.queue_daily(db, load_bindings())
    except Exception:
        db.rollback()
        logger.warning("日常表情检查失败；未重试发送")
    finally:
        db.close()


def start_scheduler() -> None:
    if scheduler.running:
        return
    scheduler.add_job(check_due_tasks, "interval", seconds=CHECK_SECONDS, id="due-task-check", replace_existing=True)
    scheduler.add_job(check_daily_stickers, "interval", seconds=30, id="daily-sticker-check", replace_existing=True)
    scheduler.start()
    logger.info("提醒调度器已启动，检查间隔 %s 秒", CHECK_SECONDS)


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("提醒调度器已停止")
