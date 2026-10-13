"""Authenticated attachment reading with shared per-owner serialization and receipts."""
import hashlib
import json
import os
import time
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from . import materials, clawbot_router
from .clawbot_bindings import load_bindings
from .clawbot_identity import clawbot_owner
from .database import get_db
from .models import ClawbotMessage, ClawbotVoiceMessage
from .schemas import ChatResponse

router = APIRouter(prefix="/api/internal/clawbot", include_in_schema=False,
                   dependencies=[Depends(clawbot_router.authenticate)])


class MaterialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    account_id: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x20\x7f]+$")
    sender_id: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x20\x7f]+$")
    message_id: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x20\x7f]+$")
    kind: Literal["image", "file"]
    filename: str = Field(min_length=1, max_length=200, pattern=r"^[^/\\\x00-\x1f\x7f]+$")
    data_base64: str = Field(min_length=1, max_length=((materials.MAX_BYTES + 2) // 3) * 4)


def check_binding(payload):
    if not any(v["account_id"] == payload.account_id and v["sender_id"] == payload.sender_id for v in load_bindings()):
        raise HTTPException(403, "接入未绑定或已撤销")


def process_material(payload, db):
    owner = clawbot_owner(payload.account_id, payload.sender_id)
    key = hashlib.sha256(f"{payload.account_id}\0{payload.sender_id}\0{payload.message_id}".encode()).hexdigest()
    data = materials.decode_data(payload.data_base64)
    signature = hashlib.sha256(json.dumps([payload.kind, payload.filename,
                    hashlib.sha256(data).hexdigest()], ensure_ascii=False).encode()).hexdigest()
    marker = f"[用户主动发送的资料；{payload.kind}；{signature}]"
    with clawbot_router._locks[int(owner[-8:], 16) % len(clawbot_router._locks)]:
        check_binding(payload)
        row = db.scalar(select(ClawbotMessage).where(ClawbotMessage.external_message_id == key))
        if row:
            if row.content != marker:
                raise HTTPException(409, "相同消息ID的内容不同，未重复识别")
            if row.status != "processed" or not row.response_json:
                raise HTTPException(409, "该附件此前识别失败或结果不确定，请发送新的消息")
            return ChatResponse.model_validate_json(row.response_json)
        if db.scalar(select(ClawbotVoiceMessage).where(ClawbotVoiceMessage.external_message_id == key)):
            raise HTTPException(409, "该消息ID已用于语音，未重复识别")
        # Validate before consuming an ID or paying for the model.
        image = materials.prepare_image(data) if payload.kind == "image" else None
        text = materials.extract_file(data, payload.filename) if payload.kind == "file" else None
        hits = clawbot_router._rate_hits[owner]
        now = time.monotonic()
        while hits and now - hits[0] >= 60:
            hits.popleft()
        if len(hits) >= clawbot_router.RATE_LIMIT:
            raise HTTPException(429, "资料发送较频繁，请一分钟后再试")
        hits.append(now)
        row = ClawbotMessage(external_message_id=key, owner_id=owner, content=marker, status="processing")
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "该消息已被其他请求领取，未重复识别") from None
        row_id = row.id
        try:
            reply = materials.summarize(text=text, image=image)
            check_binding(payload)  # Revocation can occur while the model is reading.
            result = ChatResponse(action="material_summary", parser="model",
                reply=reply if image else f"文件阅读结果：\n{reply}" + materials.FOOTER)
            row.reply_content, row.response_json = result.reply, result.model_dump_json()
            row.status, row.processed_at = "processed", datetime.now()
            db.commit()
            return result
        except Exception:
            db.rollback()
            row = db.get(ClawbotMessage, row_id)
            row.status = "failed"
            db.commit()
            raise


@router.post("/materials", response_model=ChatResponse)
async def read_material(request: Request, db: Session = Depends(get_db)):
    if os.getenv("CLAWBOT_MATERIALS_ENABLED", "true").lower() != "true":
        raise HTTPException(503, "资料识别已停用")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 14 * 1024 * 1024:
            raise HTTPException(413, "附件过大，最多10MB")
    try:
        payload = MaterialRequest.model_validate_json(bytes(raw))
    except ValidationError:
        raise HTTPException(422, "附件字段无效") from None
    check_binding(payload)
    try:
        return await run_in_threadpool(process_material, payload, db)
    except materials.MaterialError as error:
        raise HTTPException(422, str(error)) from None
