from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    due_at: datetime
    priority: str = "normal"


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    due_at: datetime
    status: str
    priority: str
    created_at: datetime
    updated_at: datetime


class SnoozeRequest(BaseModel):
    due_at: datetime


class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=1000)


class ChatResponse(BaseModel):
    reply: str
    action: str | None = None
    task: TaskRead | None = None
    parser: str = "rules"
