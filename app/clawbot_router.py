"""仅供本机微信连接器调用；不接受客户端自选 owner，不暴露公众号凭据。"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .chat_service import handle_chat_message
from .clawbot_identity import clawbot_owner
from .database import get_db
from .models import ClawbotMessage
from .schedule_access import issue_schedule_link, public_base_url
from .schemas import ChatResponse
from . import clawbot_delivery
from .clawbot_bindings import load_bindings

router = APIRouter(prefix="/api/internal/clawbot", include_in_schema=False)
logger = logging.getLogger(__name__)
_locks = [threading.Lock() for _ in range(64)]
RATE_LIMIT = 30  # 每位已绑定用户每分钟的新消息；重复投递不消耗额外模型额度。
_rate_hits: dict[str, deque[float]] = defaultdict(deque)


class BridgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    account_id: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x1f]+$")
    sender_id: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x1f]+$")
    message_id: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x1f]+$")
    content: str = Field(min_length=1, max_length=8000)


class DeliveryLease(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_token: str = Field(pattern=r"^[a-f0-9]{64}$")


class DeliveryResult(DeliveryLease):
    outcome: Literal["accepted", "failed", "unknown"]
    message_id: str | None = Field(default=None, max_length=100)
    error_code: str | None = Field(default=None, max_length=40, pattern=r"^[a-zA-Z0-9_-]+$")


def bridge_enabled() -> bool:
    return os.getenv("CLAWBOT_ENABLED", "false").lower() == "true"


def authenticate(authorization: str | None = Header(default=None)) -> None:
    if not bridge_enabled():
        raise HTTPException(503, "微信连接器未启用")
    token = os.getenv("CLAWBOT_BRIDGE_TOKEN", "")
    if len(token) < 32:
        raise HTTPException(503, "微信连接器未配置")
    supplied = (authorization or "").removeprefix("Bearer ")
    if not authorization or not authorization.startswith("Bearer ") or not hmac.compare_digest(
        supplied.encode(), token.encode()
    ):
        raise HTTPException(401, "连接器认证失败")


def wants_schedule(content: str) -> bool:
    normalized = re.sub(r"[\s，,。.!！?？]", "", content)
    return normalized in {"我的日程", "日程记录", "日程页面", "日程页"} or bool(
        re.fullmatch(r"(?:打开|查看|进入|看看)(?:我的)?(?:日程|日程记录|日程页面|日程页|日程表)", normalized)
    )


def wants_branding(content: str) -> bool:
    return re.sub(r"[\s，,。.!！?？]", "", content) in {
        "小满资料", "初始化小满", "设置小满头像", "设置小满昵称", "设置小满名字"}


def branding_reply() -> ChatResponse:
    voice = "你也可以发语音，说出待办或提醒；我会回显识别文字。" if os.getenv("ASR_ENABLED", "false").lower() == "true" else "当前音频识别未启用，请先发文字。"
    return ChatResponse(action="chat", reply="我是小满，网页和项目后台使用小满名称与猫爪图标。\n"
                        "微信中的ClawBot联系人名称和头像由微信客户端控制，当前插件没有修改接口，不能替你自动改名或换头像。\n"
                        "我可以聊天、记录提醒、查询待办和打开日程。" + voice)


def schedule_reply(db: Session, owner: str, base_url: str | None) -> ChatResponse:
    if not base_url:
        if os.getenv("CLAWBOT_PUBLIC_URL_FILE"):
            return ChatResponse(action="schedule", reply="手机日程地址暂时不可用，本机 HTTPS 隧道未就绪或已断线。请稍后再发“打开日程”；也可以直接发“查询待办”或“今天有什么任务”。")
        return ChatResponse(action="schedule", reply="当前本机没有配置手机可访问的日程网页地址。你可以直接发“查询待办”或“今天有什么任务”。")
    link = issue_schedule_link(db, owner, base_url)
    # The ClawBot client renders Markdown links but does not auto-link bare URLs.
    # Keep the bearer in the fragment; never place it in a query or server log.
    return ChatResponse(action="schedule", reply=f"[点击打开我的日程]({link})\n30 分钟内有效，仅限本人使用，请不要转发。")


def _cached(record: ClawbotMessage, payload: BridgeRequest, db: Session, base_url: str | None) -> ChatResponse:
    if record.content != payload.content:
        raise HTTPException(409, "相同消息 ID 的内容不同")
    if record.status != "processed" or not record.response_json:
        raise HTTPException(409, "消息处理结果不确定，请先查询待办，不要重复创建")
    # 授权链接不存历史；重复投递可重新签发同一用户的短期链接。
    if wants_schedule(payload.content):
        return schedule_reply(db, record.owner_id, base_url)
    return ChatResponse.model_validate_json(record.response_json)


def process_message(payload: BridgeRequest, db: Session, base_url: str | None) -> ChatResponse:
    owner = clawbot_owner(payload.account_id, payload.sender_id)
    message_key = hashlib.sha256(f"{payload.account_id}\0{payload.sender_id}\0{payload.message_id}".encode()).hexdigest()
    # 单进程按用户串行，数据库唯一键同时保证重启/重复投递不会重复执行任务。
    with _locks[int(owner[-8:], 16) % len(_locks)]:
        existing = db.scalar(select(ClawbotMessage).where(ClawbotMessage.external_message_id == message_key))
        if existing:
            return _cached(existing, payload, db, base_url)
        hits = _rate_hits[owner]
        now = time.monotonic()
        while hits and now - hits[0] >= 60:
            hits.popleft()
        if len(hits) >= RATE_LIMIT:
            raise HTTPException(429, "消息较频繁，请稍后再试", headers={"Retry-After": "60"})
        hits.append(now)
        record = ClawbotMessage(external_message_id=message_key, owner_id=owner, content=payload.content, status="processing")
        db.add(record)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.scalar(select(ClawbotMessage).where(ClawbotMessage.external_message_id == message_key))
            if existing:
                return _cached(existing, payload, db, base_url)
            raise HTTPException(409, "消息正在处理") from None
        record_id = record.id
        try:
            if wants_branding(payload.content):
                response = branding_reply()
            elif wants_schedule(payload.content):
                response = schedule_reply(db, owner, base_url)
            else:
                response = handle_chat_message(payload.content, db, conversation_id=owner)
            saved = response.model_copy(update={"reply": "[日程链接已发送；授权令牌未保存]"}) if wants_schedule(payload.content) and base_url else response
            record.reply_content = saved.reply
            record.response_json = saved.model_dump_json()
            record.processed_at = datetime.now()
            record.status = "processed"
            db.commit()
            logger.info("clawbot message processed record_id=%s action=%s", record_id, response.action)
            return response
        except Exception as exc:
            db.rollback()
            record = db.get(ClawbotMessage, record_id)
            if record:
                record.status = "failed"
                db.commit()
            # 不记录内容、用户 ID、授权链接或可能含密钥的异常正文。
            logger.warning("clawbot processing failed record_id=%s error_type=%s", record_id, type(exc).__name__)
            raise HTTPException(502, "小满处理结果不确定，请先查询待办，不要重复创建") from None


@router.get("/health", dependencies=[Depends(authenticate)])
def health():
    return {"status": "ok", "service": "xiaoman-clawbot", "bindings": len(load_bindings()),
            "instance": hashlib.sha256(os.environ["CLAWBOT_BRIDGE_TOKEN"].encode()).hexdigest()[:16],
            "proactive_reminders": clawbot_delivery.enabled(), "reminders": clawbot_delivery.status()}


@router.post("/outbox/claim", dependencies=[Depends(authenticate)])
def claim_delivery(db: Session = Depends(get_db)):
    return {"delivery": clawbot_delivery.claim(db, load_bindings())}


@router.post("/outbox/{delivery_id}/start", dependencies=[Depends(authenticate)])
def start_delivery(delivery_id: int, payload: DeliveryLease, db: Session = Depends(get_db)):
    clawbot_delivery.begin_send(db, delivery_id, payload.claim_token, load_bindings())
    return {"send": True}


@router.post("/outbox/{delivery_id}/result", dependencies=[Depends(authenticate)])
def result_delivery(delivery_id: int, payload: DeliveryResult, db: Session = Depends(get_db)):
    clawbot_delivery.complete_send(db, delivery_id, payload.claim_token, payload.outcome, payload.message_id, payload.error_code)
    return {"ok": True}


@router.post("/chat", response_model=ChatResponse, dependencies=[Depends(authenticate)])
async def chat(request: Request, db: Session = Depends(get_db)):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 32768:
            raise HTTPException(413, "消息过长")
    try:
        payload = BridgeRequest.model_validate_json(bytes(raw))
    except ValidationError:
        raise HTTPException(422, "消息字段无效") from None
    if not payload.content.strip():
        raise HTTPException(422, "消息不能为空")
    if not any(v["account_id"] == payload.account_id and v["sender_id"] == payload.sender_id for v in load_bindings()):
        raise HTTPException(403, "用户未绑定")
    return await run_in_threadpool(process_message, payload, db, public_base_url(request))
