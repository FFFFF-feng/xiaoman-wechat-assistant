from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .adapters.wechat_dialogue import (
    decrypt_dialogue_text,
    encrypt_dialogue_text,
    verify_dialogue_signature,
    wechat_dialogue_status,
)
from .chat_service import handle_chat_message
from .database import get_db
from .model_parser import generate_companion_reply
from .models import WechatDialogueMessage
from .parser import parse_command
from .schemas import ChatResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/channels/wechat-dialogue", tags=["微信对话开放平台"])
MAX_BODY_BYTES = 256 * 1024
FALLBACK_INTENT_NAME = "闲聊兜底"


def _quick_companion_response(query: str) -> ChatResponse:
    """在微信平台 2 秒时限内给普通聊天稳定返回一条陪伴回复。

    云端模型的实际首包时间可能超过平台的同步回调上限，所以默认不在
    微信回调中等待模型；网页聊天仍使用完整模型。部署低延迟推理服务后，
    可显式开启 WECHAT_DIALOGUE_FAST_MODEL_ENABLED 再启用此处模型调用。
    """
    fast_model_enabled = os.getenv(
        "WECHAT_DIALOGUE_FAST_MODEL_ENABLED", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    model_reply = (
        generate_companion_reply(query, timeout_seconds=0.6)
        if fast_model_enabled
        else None
    )
    normalized = query.strip()
    if model_reply:
        reply = model_reply
    elif any(word in normalized for word in ("累", "难过", "烦", "压力", "焦虑")):
        reply = "听起来你今天扛了不少事。先缓一口气吧，我在这儿陪你慢慢说。"
    elif any(word in normalized for word in ("你好", "嗨", "在吗")):
        reply = "你好呀，我在呢。想聊聊天，还是要我帮你记一个日程？"
    else:
        reply = "我在呢。你可以慢慢说，今天想聊什么，或者要我帮你记一件事吗？"
    return ChatResponse(
        reply=reply,
        action="chat",
        parser="model" if model_reply else "rules",
    )


def _is_companion_request(intent_name: str, query: str) -> bool:
    """兼容平台调试时未传或改写 IntentName 的情况。

    只要本地规则无法将文本判断为任务操作，就按普通聊天回复；
    明确的创建、查询、完成、延后、取消仍保留原日程处理路径。
    """
    return intent_name == FALLBACK_INTENT_NAME or parse_command(query).action == "unknown"


def _enabled() -> bool:
    return os.getenv("WECHAT_DIALOGUE_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _require_encryption() -> bool:
    return os.getenv("WECHAT_DIALOGUE_REQUIRE_ENCRYPTION", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _field(payload: dict[str, Any], name: str) -> Any:
    if name in payload:
        return payload[name]
    lowered = name.lower()
    for key, value in payload.items():
        if str(key).lower() == lowered:
            return value
    return None


def _validate_timestamp(timestamp: str) -> None:
    configured = os.getenv("WECHAT_DIALOGUE_MAX_SKEW_SECONDS", "300").strip()
    try:
        max_skew = max(0, int(configured or "300"))
        numeric = float(timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Timestamp 格式无效") from exc
    if numeric > 10_000_000_000:
        numeric /= 1000
    if max_skew and abs(time.time() - numeric) > max_skew:
        raise HTTPException(status_code=403, detail="请求时间已过期")


def _answer_payload(answer: str) -> dict[str, Any]:
    return {
        "answer_type": "text",
        "text_info": {"short_answer": answer[:2000]},
    }


def _build_response(answer: str, encrypted: bool) -> Response:
    payload = _answer_payload(answer)
    if encrypted:
        raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return PlainTextResponse(encrypt_dialogue_text(raw_json))
    return JSONResponse(payload)


def _decode_request_body(body: bytes) -> tuple[dict[str, Any], bool]:
    if not body or len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=400, detail="请求体为空或过大")
    try:
        decoded = body.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="请求体不是 UTF-8 文本") from exc

    parsed: Any = None
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        encrypted_value = _field(parsed, "encrypt")
        if encrypted_value is None:
            return parsed, False
        decoded = _text(encrypted_value)
    elif isinstance(parsed, str):
        decoded = parsed.strip()

    try:
        decrypted = decrypt_dialogue_text(decoded)
        payload = json.loads(decrypted)
    except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
        logger.warning("微信对话开放平台请求解密或解析失败：%s", type(exc).__name__)
        raise HTTPException(status_code=400, detail="加密请求格式无效") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="解密后的请求必须是 JSON 对象")
    return payload, True


@router.get("/status", summary="查看微信对话开放平台通道状态")
def channel_status():
    return wechat_dialogue_status()


@router.post("/task", summary="接收微信对话开放平台第三方服务请求")
async def receive_dialogue_request(request: Request, db: Session = Depends(get_db)):
    if not _enabled():
        raise HTTPException(status_code=503, detail="微信对话开放平台通道尚未启用")
    if not os.getenv("WECHAT_DIALOGUE_TOKEN", "").strip():
        raise HTTPException(status_code=503, detail="尚未配置 WECHAT_DIALOGUE_TOKEN")

    payload, encrypted = _decode_request_body(await request.body())
    if _require_encryption() and not encrypted:
        raise HTTPException(status_code=400, detail="当前接口只接受加密请求")

    request_id = _text(_field(payload, "RequestId"))
    session_id = _text(_field(payload, "SessionId"))
    query = _text(_field(payload, "Query"))
    skill_name = _text(_field(payload, "SkillName"))
    intent_name = _text(_field(payload, "IntentName"))
    timestamp = _text(_field(payload, "Timestamp"))
    signature = _text(_field(payload, "Signature"))
    user_id = _text(_field(payload, "UserId"))
    if not request_id or not query or not timestamp or not signature or not user_id:
        raise HTTPException(status_code=400, detail="请求缺少 RequestId、Query、Timestamp、Signature 或 UserId")
    _validate_timestamp(timestamp)
    if not verify_dialogue_signature(
        signature,
        timestamp,
        skill_name,
        intent_name,
        query,
    ):
        raise HTTPException(status_code=403, detail="微信对话开放平台签名校验失败")

    existing = db.scalar(
        select(WechatDialogueMessage).where(
            WechatDialogueMessage.external_request_id == request_id
        )
    )
    if existing and existing.reply_content:
        return _build_response(existing.reply_content, encrypted)

    if existing is None:
        record = WechatDialogueMessage(
            external_request_id=request_id,
            user_id=user_id,
            session_id=session_id,
            skill_name=skill_name,
            intent_name=intent_name,
            content=query,
            status="received",
        )
        db.add(record)
        try:
            db.commit()
            db.refresh(record)
        except IntegrityError:
            db.rollback()
            existing = db.scalar(
                select(WechatDialogueMessage).where(
                    WechatDialogueMessage.external_request_id == request_id
                )
            )
            if existing and existing.reply_content:
                return _build_response(existing.reply_content, encrypted)
            raise HTTPException(status_code=409, detail="请求正在处理中")
    else:
        record = existing

    try:
        record.status = "processing"
        db.commit()
        if _is_companion_request(intent_name, query):
            # 平台已经把普通聊天路由到此意图。用短模型调用争取自然回复，
            # 并在约 1 秒后降级，确保不触发平台 2 秒回调超时。
            result = _quick_companion_response(query)
        else:
            # 日程技能优先走本地规则解析，避免任务回调被模型时延阻塞。
            result = handle_chat_message(
                query,
                db,
                conversation_id=f"wechat-dialogue:{user_id}",
                use_model=False,
            )
        record = db.get(WechatDialogueMessage, record.id)
        if record is None:
            raise RuntimeError("微信对话开放平台消息记录不存在")
        record.reply_content = result.reply
        record.status = "processed"
        record.processed_at = datetime.now()
        db.commit()
        return _build_response(result.reply, encrypted)
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        record = db.get(WechatDialogueMessage, record.id)
        if record:
            record.status = "failed"
            record.error = f"{type(exc).__name__}: {str(exc)[:300]}"
            record.processed_at = datetime.now()
            db.commit()
        logger.exception("处理微信对话开放平台请求失败 request_id=%s", request_id)
        raise HTTPException(status_code=500, detail="小满暂时打了个盹，请稍后再试") from exc
