from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

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
    category: str = "life"
    notes: str = ""
    reminder_enabled: bool = True
    reminder_channel: str | None = None
    created_at: datetime
    updated_at: datetime


class SnoozeRequest(BaseModel):
    due_at: datetime


class ScheduleTaskFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=300)
    due_at: datetime
    priority: Literal["low", "normal", "high"] = "normal"
    category: Literal["life", "work", "study", "health"] = "life"
    notes: str = Field(default="", max_length=2000)
    reminder_enabled: bool = Field(default=True, strict=True)


class ScheduleTaskCreate(ScheduleTaskFields):
    request_id: UUID


class ScheduleTaskEdit(ScheduleTaskFields):
    expected_updated_at: datetime


class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=1000)


class ChatResponse(BaseModel):
    reply: str
    action: str | None = None
    task: TaskRead | None = None
    parser: str = "rules"
