from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .adapters.wechat_official import wechat_channel_status
from .adapters.wechat_dialogue import wechat_dialogue_status
from .chat_service import handle_chat_message, record_message
from .clawbot_router import router as clawbot_router, bridge_enabled
from .clawbot_delivery import status as clawbot_reminder_status
from .database import get_db, init_db
from .model_parser import model_status
from .models import Reminder, Task
from .parser import now_local
from .scheduler import scheduler, start_scheduler, stop_scheduler
from .schedule_router import router as schedule_router
from .schedule_page import schedule_page_response
from .clawbot_enrollment import public_router as join_router, admin_router as enrollment_router
from .join_page import join_page_response
from .clawbot_voice import router as voice_router
from .schemas import ChatRequest, ChatResponse, SnoozeRequest, TaskCreate, TaskRead
from .wechat_router import router as wechat_router
from .wechat_dialogue_router import router as wechat_dialogue_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
# 获取公众号 access_token 时 AppSecret 位于请求参数中，不允许 httpx INFO 日志输出完整 URL。
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent.parent
LOCAL_TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Shanghai"))


def _as_local_naive(value: datetime) -> datetime:
    """将 API 的带时区时间统一换算为本地时区的 SQLite 兼容时间。"""
    if value.tzinfo is not None:
        return value.astimezone(LOCAL_TZ).replace(tzinfo=None)
    return value


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(
    title="微信小宠物 · 夜巡猫窝",
    version="0.2.0",
    description="支持日常陪伴聊天和任务提醒的本地宠物雏形",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(wechat_router)
app.include_router(wechat_dialogue_router)
app.include_router(schedule_router)
app.include_router(clawbot_router)
app.include_router(join_router)
app.include_router(enrollment_router)
app.include_router(voice_router)
app.mount("/static", StaticFiles(directory=BASE_DIR / "web"), name="static")
# 提前建表，确保 reload 或直接导入应用时 API 也能立即使用。
init_db()


def _task_or_404(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if not task or task.owner_id is not None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(BASE_DIR / "web" / "index.html")


@app.get("/schedule", include_in_schema=False)
def schedule_page():
    return schedule_page_response()


@app.get("/join", include_in_schema=False)
def join_page():
    return join_page_response()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "wechat-pet",
        "version": app.version,
        "scheduler_running": scheduler.running,
        "model": model_status(),
        "channels": {
            "mock": {"enabled": True},
            "wechat_official": wechat_channel_status(),
            "wechat_dialogue": wechat_dialogue_status(),
            "clawbot": {"enabled": bridge_enabled(), "reminders": clawbot_reminder_status()},
        },
        "time": datetime.now().isoformat(),
    }


@app.get("/api/tasks", response_model=list[TaskRead])
def list_tasks(
    status: str | None = Query(default=None),
    today: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    # 本地模拟界面只显示本地任务，不能通过公开的调试接口读取微信用户日程。
    query = select(Task).where(Task.owner_id.is_(None)).order_by(Task.due_at.asc())
    if status:
        query = query.where(Task.status == status)
    if today:
        start = datetime.combine(now_local().date(), time.min)
        end = start + timedelta(days=1)
        query = query.where(Task.due_at >= start, Task.due_at < end)
    return db.scalars(query).all()


@app.post("/api/tasks", response_model=TaskRead, status_code=201)
def create_task(payload: TaskCreate, db: Session = Depends(get_db)):
    due_at = _as_local_naive(payload.due_at)
    task = Task(title=payload.title.strip(), due_at=due_at, priority=payload.priority, status="open")
    db.add(task)
    db.commit()
    db.refresh(task)
    record_message(db, "system", f"创建任务：{task.title}")
    db.commit()
    return task


@app.post("/api/tasks/{task_id}/complete", response_model=TaskRead)
def complete_task(task_id: int, db: Session = Depends(get_db)):
    task = _task_or_404(db, task_id)
    task.status = "done"
    task.updated_at = datetime.now()
    record_message(db, "system", f"完成任务：{task.title}")
    db.commit()
    db.refresh(task)
    return task


@app.post("/api/tasks/{task_id}/snooze", response_model=TaskRead)
def snooze_task(task_id: int, payload: SnoozeRequest, db: Session = Depends(get_db)):
    task = _task_or_404(db, task_id)
    task.due_at = _as_local_naive(payload.due_at)
    task.status = "snoozed"
    task.updated_at = datetime.now()
    record_message(db, "system", f"延后任务：{task.title}")
    db.commit()
    db.refresh(task)
    return task


@app.post("/api/tasks/{task_id}/cancel", response_model=TaskRead)
def cancel_task(task_id: int, db: Session = Depends(get_db)):
    task = _task_or_404(db, task_id)
    task.status = "cancelled"
    task.updated_at = datetime.now()
    record_message(db, "system", f"取消任务：{task.title}")
    db.commit()
    db.refresh(task)
    return task


@app.post("/api/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, db: Session = Depends(get_db)):
    try:
        return handle_chat_message(payload.content, db)
    except Exception:
        db.rollback()
        logger.exception("处理聊天消息失败")
        raise HTTPException(status_code=500, detail="猫咪暂时打了个盹，请稍后再试")


@app.get("/api/notifications")
def notifications(limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)):
    reminders = db.scalars(
        select(Reminder)
        .join(Task, Reminder.task_id == Task.id)
        .where(Task.owner_id.is_(None))
        .order_by(desc(Reminder.id))
        .limit(limit)
    ).all()
    return [
        {
            "id": reminder.id,
            "task_id": reminder.task_id,
            "title": reminder.task.title if reminder.task else "未知任务",
            "reminder_time": reminder.reminder_time,
            "sent_at": reminder.sent_at,
            "status": reminder.status,
            "channel": (
                "wechat"
                if reminder.task and reminder.task.owner_id and reminder.task.owner_id.startswith("wechat:")
                else "local"
            ),
        }
        for reminder in reminders
    ]
