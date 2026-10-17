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
from . import materials
from . import record_import
from . import clawbot_welcome
from . import clawbot_stickers

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
    if normalized in {"我的日程", "日程记录", "日程页面", "日程页", "我的日历", "日历页面", "日历页"}:
        return True
    # Match complete requests, including polite/conversational wrappers. Do not
    # strip negation, quotes, dates or extra instructions into an opening request.
    prefix = r"(?:小满)?(?:(?:请|麻烦你?|劳驾|能不能|可不可以|可以|能|帮我|替我|给我|我想要|我想|我要)){0,5}"
    target = r"(?:我的|我自己的)?(?:日程(?:记录|页面|页|表)?|日历(?:页面|页)?)(?:链接|入口)?"
    action = r"(?:打开|查看|看看|看|进入|访问|跳转到)(?:一下|一眼|下)?"
    request = (
        rf"(?:{action}{target}"
        rf"|(?:把|将){target}(?:给我)?(?:打开|调出来)(?:一下|下)?"
        r"|(?:发我|发给我|给我|发一下|发下|发送|发)(?:我的)?(?:日程|日历)(?:链接|入口)(?:给我)?)"
    )
    suffix = r"(?:一下|看看|给我看看|让我看看)?(?:吧|呀|啊|呢|吗|好吗|好么|可以吗|行吗|可以么)?"
    return re.fullmatch(prefix + request + suffix, normalized) is not None


def wants_branding(content: str) -> bool:
    return re.sub(r"[\s，,。.!！?？]", "", content) in {
        "小满资料", "初始化小满", "设置小满头像", "设置小满昵称", "设置小满名字",
        "使用指南", "新手指南", "小满指南", "功能介绍", "怎么使用小满"}


def branding_reply(base_url: str | None = None) -> ChatResponse:
    return ChatResponse(action="chat", reply=clawbot_welcome.guide_reply(base_url))


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
    cached = ChatResponse.model_validate_json(record.response_json)
    if cached.action == "schedule" and wants_schedule(payload.content):
        return schedule_reply(db, record.owner_id, base_url)
    if wants_branding(payload.content):
        return branding_reply(base_url)
    return cached


def process_message(payload: BridgeRequest, db: Session, base_url: str | None) -> ChatResponse:
    owner = clawbot_owner(payload.account_id, payload.sender_id)
    message_key = hashlib.sha256(f"{payload.account_id}\0{payload.sender_id}\0{payload.message_id}".encode()).hexdigest()
    # 单进程按用户串行，数据库唯一键同时保证重启/重复投递不会重复执行任务。
    with _locks[int(owner[-8:], 16) % len(_locks)]:
        clawbot_welcome.activate(db, owner)
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
            clawbot_stickers.touch(db, owner)
            imported = record_import.collect(db, owner, payload.content)
            if imported is not None:
                if not any(v["account_id"] == payload.account_id and v["sender_id"] == payload.sender_id for v in load_bindings()):
                    raise HTTPException(403, "接入已撤销")
                response = imported
            elif wants_branding(payload.content):
                response = branding_reply(base_url)
            elif wants_schedule(payload.content):
                response = schedule_reply(db, owner, base_url)
            elif (sticker_response := clawbot_stickers.command(db, owner, payload.content, message_key)) is not None:
                response = sticker_response
            elif materials.wants_record_import(payload.content):
                if os.getenv("CLAWBOT_MATERIALS_ENABLED", "true").lower() != "true":
                    response = ChatResponse(action="material_error", reply="资料识别已停用，请先发普通文字。")
                else:
                    try:
                        reply = materials.record_summary(payload.content)
                        # Recheck authorization after the external model call.
                        if not any(v["account_id"] == payload.account_id and v["sender_id"] == payload.sender_id for v in load_bindings()):
                            raise HTTPException(403, "接入已撤销")
                        response = ChatResponse(action="material_summary", parser="model", reply=reply + materials.FOOTER)
                    except materials.MaterialError as error:
                        response = ChatResponse(action="material_error", reply=str(error) + "；未创建提醒。")
            else:
                response = handle_chat_message(payload.content, db, conversation_id=owner)
                if response.action == "chat":
                    clawbot_stickers.maybe_chat(db, owner, payload.content, message_key)
            saved = response.model_copy(update={"reply": "[日程链接已发送；授权令牌未保存]"}) if response.action == "schedule" and base_url else response
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
            "proactive_reminders": clawbot_delivery.enabled(), "reminders": clawbot_delivery.status(),
            "stickers_enabled": clawbot_stickers.enabled()}


@router.post("/stickers/claim", dependencies=[Depends(authenticate)])
def claim_sticker(db: Session = Depends(get_db)):
    return {"delivery": clawbot_stickers.claim(db, load_bindings())}


@router.post("/stickers/{delivery_id}/start", dependencies=[Depends(authenticate)])
def start_sticker(delivery_id: int, payload: DeliveryLease, db: Session = Depends(get_db)):
    clawbot_stickers.begin_send(db, delivery_id, payload.claim_token, load_bindings())
    return {"send": True}


@router.post("/stickers/{delivery_id}/result", dependencies=[Depends(authenticate)])
def result_sticker(delivery_id: int, payload: DeliveryResult, db: Session = Depends(get_db)):
    clawbot_stickers.complete_send(db, delivery_id, payload.claim_token, payload.outcome,
                                 payload.message_id, payload.error_code)
    return {"ok": True}


@router.post("/outbox/claim", dependencies=[Depends(authenticate)])
def claim_delivery(db: Session = Depends(get_db)):
    return {"delivery": clawbot_delivery.claim(db, load_bindings())}


@router.post("/welcome/claim", dependencies=[Depends(authenticate)])
def claim_welcome(request: Request, db: Session = Depends(get_db)):
    return {"delivery": clawbot_welcome.claim(db, load_bindings(), public_base_url(request))}


@router.post("/welcome/{delivery_id}/start", dependencies=[Depends(authenticate)])
def start_welcome(delivery_id: int, payload: DeliveryLease, db: Session = Depends(get_db)):
    clawbot_welcome.begin_send(db, delivery_id, payload.claim_token, load_bindings())
    return {"send": True}


@router.post("/welcome/{delivery_id}/result", dependencies=[Depends(authenticate)])
def result_welcome(delivery_id: int, payload: DeliveryResult, db: Session = Depends(get_db)):
    clawbot_welcome.complete_send(db, delivery_id, payload.claim_token, payload.outcome,
                                 payload.message_id, payload.error_code)
    return {"ok": True}


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
