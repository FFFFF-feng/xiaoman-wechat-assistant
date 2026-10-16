from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/tasks.db")
if DATABASE_URL.startswith("sqlite:///./"):
    database_path = BASE_DIR / DATABASE_URL.replace("sqlite:///./", "", 1)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    DATABASE_URL = f"sqlite:///{database_path.as_posix()}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    if engine.dialect.name == "sqlite":
        _migrate_sqlite()


def _migrate_sqlite() -> None:
    """为已有本地数据库补充可空字段，不删除或重建用户数据。"""
    with engine.begin() as connection:
        additions = {
            "tasks": {"owner_id": "VARCHAR(200)", "wechat_app_id": "VARCHAR(64)",
                      "category": "VARCHAR(20) NOT NULL DEFAULT 'life'",
                      "notes": "TEXT NOT NULL DEFAULT ''", "reminder_enabled": "BOOLEAN NOT NULL DEFAULT 1",
                      "reminder_channel": "VARCHAR(30)", "notification_template_id": "VARCHAR(200)"},
            "reminders": {"channel": "VARCHAR(30)", "error": "TEXT", "error_code": "VARCHAR(40)",
                          "external_message_id": "VARCHAR(100)"},
            "wechat_messages": {"app_id": "VARCHAR(64)"},
            "wechat_subscriptions": {"last_event_at": "DATETIME", "last_event_rejected": "BOOLEAN NOT NULL DEFAULT 0"},
        }
        for table, fields in additions.items():
            columns = {row[1] for row in connection.execute(text(f"PRAGMA table_info({table})"))}
            for name, sql_type in fields.items():
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_tasks_owner_id ON tasks (owner_id)")
        )
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_reminders_external_message_id ON reminders (external_message_id)"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
