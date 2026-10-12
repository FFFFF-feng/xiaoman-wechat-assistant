"""Private voice bridge, with durable deduplication before ASR or task operations."""
import hashlib
import threading
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from . import speech
from .clawbot_bindings import load_bindings
from .clawbot_identity import clawbot_owner
from .clawbot_router import BridgeRequest, authenticate, process_message
from .database import get_db
from .models import ClawbotVoiceMessage
from .schedule_access import public_base_url

router = APIRouter(prefix="/api/internal/clawbot", include_in_schema=False,
                   dependencies=[Depends(authenticate)])
_locks = [threading.Lock() for _ in range(64)]
_rate_hits = defaultdict(deque)


class VoiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    account_id: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x20\x7f]+$")
    sender_id: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x20\x7f]+$")
    message_id: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x20\x7f]+$")
    audio_base64: str = Field(min_length=1, max_length=((speech.MAX_AUDIO_BYTES + 2) // 3) * 4)


def process_voice(payload, db, base_url):
    owner = clawbot_owner(payload.account_id, payload.sender_id)
    key = hashlib.sha256(f"{payload.account_id}\0{payload.sender_id}\0{payload.message_id}".encode()).hexdigest()
    signature = hashlib.sha256(payload.audio_base64.encode()).hexdigest()
    with _locks[int(owner[-8:], 16) % len(_locks)]:
        row = db.scalar(select(ClawbotVoiceMessage).where(ClawbotVoiceMessage.external_message_id == key))
        if row:
            if row.audio_hash != signature:
                raise HTTPException(409, "相同消息ID的语音不同，未执行操作")
            if row.status != "processed" or not row.transcript:
                raise HTTPException(409, "语音处理失败或结果不确定，请先查询待办，再发文字")
        else:
            speech.configuration()  # Disabled/missing ASR does not consume the message ID.
            audio = speech.validate_audio(payload.audio_base64)
            hits = _rate_hits[owner]
            now = time.monotonic()
            while hits and now - hits[0] >= 60:
                hits.popleft()
            if len(hits) >= 10:
                raise HTTPException(429, "语音较频繁，请等一分钟再试")
            hits.append(now)
            # Do not spend ASR fees on an ID already used by a text message.
            from .models import ClawbotMessage
            if db.scalar(select(ClawbotMessage).where(ClawbotMessage.external_message_id == key)):
                raise HTTPException(409, "该消息ID已处理，未执行语音操作")
            row = ClawbotVoiceMessage(external_message_id=key, owner_id=owner, audio_hash=signature)
            db.add(row); db.commit()
            try:
                row.transcript = speech.transcribe(audio)
                db.commit()
            except speech.SpeechError as error:
                row.status, row.error_code = "failed", error.code
                db.commit()
                raise
        try:
            # Recheck revocation after the ASR network wait, before reading or mutating tasks.
            if not any(v["account_id"] == payload.account_id and v["sender_id"] == payload.sender_id for v in load_bindings()):
                raise HTTPException(403, "接入已撤销，未执行语音意图")
            command = BridgeRequest(account_id=payload.account_id, sender_id=payload.sender_id,
                                    message_id=payload.message_id, content=speech.intent_text(row.transcript))
            result = process_message(command, db, base_url)
            row.status = "processed"
            db.commit()
            return result.model_copy(update={"reply": f"语音识别：{row.transcript}\n\n{result.reply}"})
        except Exception:
            db.rollback()
            row = db.scalar(select(ClawbotVoiceMessage).where(ClawbotVoiceMessage.external_message_id == key))
            row.status, row.error_code = "failed", "speech_operation_uncertain"
            db.commit()
            raise


@router.post("/voice")
async def voice(request: Request, db: Session = Depends(get_db)):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 6 * 1024 * 1024:
            raise HTTPException(413, "语音过大，请发送短语音或文字")
    try:
        payload = VoiceRequest.model_validate_json(bytes(raw))
    except ValidationError:
        raise HTTPException(422, "语音字段无效") from None
    if not any(v["account_id"] == payload.account_id and v["sender_id"] == payload.sender_id for v in load_bindings()):
        raise HTTPException(403, "用户未绑定")
    try:
        return await run_in_threadpool(process_voice, payload, db, public_base_url(request))
    except speech.SpeechError as error:
        disabled = error.code in {"speech_disabled", "speech_not_configured", "speech_bad_configuration"}
        raise HTTPException(503 if disabled else 422,
                            "语音识别未配置，请先发文字。" if disabled else "语音未识别成功，未执行操作；请发文字重试。") from None
