from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), default="open", nullable=False, index=True)
    priority: Mapped[str] = mapped_column(String(20), default="normal", nullable=False)
    owner_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    wechat_app_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reminder_channel: Mapped[str | None] = mapped_column(String(30), nullable=True)
    notification_template_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    reminders: Mapped[list["Reminder"]] = relationship(back_populates="task", cascade="all, delete-orphan")


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), nullable=False, index=True)
    reminder_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    channel: Mapped[str | None] = mapped_column(String(30), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    external_message_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)

    task: Mapped[Task] = relationship(back_populates="reminders")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_type: Mapped[str] = mapped_column(String(30), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False, index=True)


class WechatMessage(Base):
    """微信公众号回调记录：用于排重、排障和发送状态追踪。"""

    __tablename__ = "wechat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_message_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    from_user: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    app_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    to_user: Mapped[str] = mapped_column(String(160), nullable=False)
    message_type: Mapped[str] = mapped_column(String(30), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="received", nullable=False, index=True)
    reply_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ClawbotMessage(Base):
    """真实微信插件入站消息；独立去重、历史和任务归属，不混用公众号记录。"""

    __tablename__ = "clawbot_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_message_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    reply_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="processing", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ClawbotDelivery(Base):
    """持久主动提醒队列。发送开始后不自动重试，避免不确定结果导致重复通知。"""
    __tablename__ = "clawbot_deliveries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reminder_id: Mapped[int] = mapped_column(ForeignKey("reminders.id"), unique=True, nullable=False)
    owner_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    claim_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_message_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)


class ClawbotInvitation(Base):
    """Single-user enrollment capability. No raw invitation, Weixin token or pairing code."""
    __tablename__ = "clawbot_invitations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(60), nullable=False)
    state: Mapped[str] = mapped_column(String(30), default="issued", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    lease_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consented_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    qr_image: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    owner_id: Mapped[str | None] = mapped_column(String(200), nullable=True)


class WechatUser(Base):
    """按 AppID 隔离用户及客服消息窗口，OpenID 不跨账号迁移。"""
    __tablename__ = "wechat_users"
    __table_args__ = (UniqueConstraint("app_id", "openid"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    openid: Mapped[str] = mapped_column(String(160), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    customer_messages_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class WechatSubscription(Base):
    """只保存经签名回调确认的一次性订阅额度，不假设拥有长期订阅权限。"""
    __tablename__ = "wechat_subscriptions"
    __table_args__ = (UniqueConstraint("app_id", "openid", "template_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    openid: Mapped[str] = mapped_column(String(160), nullable=False)
    template_id: Mapped[str] = mapped_column(String(200), nullable=False)
    credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_event_rejected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


class WechatDeliveryReceipt(Base):
    """独立保存异步送达回执，兼容回执先于 API 返回的顺序。"""
    __tablename__ = "wechat_delivery_receipts"
    __table_args__ = (UniqueConstraint("app_id", "channel", "external_message_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    external_message_id: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


class ScheduleAccess(Base):
    """微信聊天签发的短时日程访问凭证；只保存随机令牌的哈希。"""

    __tablename__ = "schedule_access"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class WechatDialogueMessage(Base):
    """微信对话开放平台第三方接口记录：用于排重、上下文和排障。"""

    __tablename__ = "wechat_dialogue_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_request_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    skill_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    intent_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="received", nullable=False, index=True)
    reply_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
