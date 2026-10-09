from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from datetime import datetime
from xml.etree.ElementTree import Element, SubElement, fromstring, tostring

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .adapters.wechat_official import wechat_adapter
from .chat_service import handle_chat_message
from .database import SessionLocal
from .models import WechatMessage
from .schedule_access import issue_schedule_link, public_base_url
from .wechat_crypto import WechatCrypto, WechatCryptoError, encryption_mode
from .wechat_notifications import NotificationBlocked, app_id, handle_event, note_inbound, owner_for, send_customer

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/channels/wechat", tags=["微信公众号"])


def _wechat_token() -> str:
    return os.getenv("WECHAT_TOKEN", "").strip()


def verify_wechat_signature(signature: str, timestamp: str, nonce: str) -> bool:
    token = _wechat_token()
    if not token or not signature or not timestamp or not nonce:
        return False
    digest = hashlib.sha1("".join(sorted([token, timestamp, nonce])).encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, signature)


def parse_wechat_xml(xml_bytes: bytes) -> dict[str, str]:
    if len(xml_bytes) > 256 * 1024:
        raise ValueError("微信消息体过大")
    upper_xml = xml_bytes.upper()
    if b"<!DOCTYPE" in upper_xml or b"<!ENTITY" in upper_xml:
        raise ValueError("不允许 DTD 或实体声明")
    root = fromstring(xml_bytes)
    if root.tag != "xml" or len({child.tag for child in root}) != len(root):
        raise ValueError("微信 XML 根节点或字段无效")
    return {child.tag: tostring(child, encoding="unicode") if len(child) else child.text or "" for child in root}


def build_text_reply(to_user: str, from_user: str, content: str) -> bytes:
    root = Element("xml")
    SubElement(root, "ToUserName").text = to_user
    SubElement(root, "FromUserName").text = from_user
    SubElement(root, "CreateTime").text = str(int(datetime.now().timestamp()))
    SubElement(root, "MsgType").text = "text"
    SubElement(root, "Content").text = content[:2000]
    return tostring(root, encoding="utf-8", xml_declaration=False)


def _external_message_id(payload: dict[str, str]) -> str:
    # 账号隔离；事件无 MsgId 时纳入嵌套订阅字段和送达状态，避免同秒事件冲突。
    stable = app_id() + "|" + (payload.get("MsgId") or repr(sorted(payload.items())))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _reply_mode() -> str:
    configured = os.getenv("WECHAT_REPLY_MODE", "auto").strip().lower()
    if configured not in {"auto", "passive", "async"}:
        configured = "auto"
    if configured == "auto":
        return "async" if wechat_adapter.configured else "passive"
    return configured


def _wants_schedule_page(text: str) -> bool:
    normalized = re.sub(r"[\s，,。.!！?？]", "", text)
    return normalized in {"我的日程", "日程记录", "日程页面", "日程页"} or bool(
        re.fullmatch(r"(?:打开|查看|进入|看看)(?:我的)?(?:日程|日程记录|日程页面|日程页|日程表)", normalized)
    )


def _process_message(record_id: int, base_url: str | None = None) -> tuple[str, str]:
    db = SessionLocal()
    try:
        record = db.get(WechatMessage, record_id)
        if not record:
            raise RuntimeError("微信消息记录不存在")
        if record.app_id and record.app_id != app_id():
            raise RuntimeError("公众号已经切换，不处理旧账号的消息")
        record.status = "processing"
        db.commit()

        if record.message_type != "text":
            reply = "小满第一阶段先支持文字消息。你可以直接输入日程或提醒内容。"
        elif _wants_schedule_page(record.content):
            if base_url:
                link = issue_schedule_link(db, owner_for(record.from_user, record.app_id), base_url)
                reply = f"你的日程在这里（30分钟内有效）：\n{link}\n这个链接能查看你的日程，请不要转发。"
            else:
                reply = "日程页面还没有可用的公网地址。请配置 PUBLIC_BASE_URL，或通过 Cloudflare 临时隧道接入后再试。"
        else:
            reply = handle_chat_message(
                record.content,
                db,
                conversation_id=owner_for(record.from_user, record.app_id),
            ).reply

        record = db.get(WechatMessage, record_id)
        if not record:
            raise RuntimeError("微信消息记录不存在")
        # 访问令牌只发给微信用户，不把完整链接明文写入聊天记录。
        record.reply_content = "[日程访问链接已发送]" if _wants_schedule_page(record.content) and base_url else reply
        record.status = "processed"
        record.processed_at = datetime.now()
        db.commit()
        return record.from_user, reply
    except Exception as exc:
        db.rollback()
        record = db.get(WechatMessage, record_id)
        if record:
            record.status = "failed"
            record.error = f"{type(exc).__name__}: {str(exc)[:300]}"
            record.processed_at = datetime.now()
            db.commit()
        logger.exception("处理微信公众号消息失败 record_id=%s", record_id)
        raise
    finally:
        db.close()


def _process_and_send(record_id: int, base_url: str | None = None) -> None:
    try:
        recipient, reply = _process_message(record_id, base_url)
        db = SessionLocal()
        try:
            send_customer(db, recipient, reply)
            record = db.get(WechatMessage, record_id)
            if record:
                record.status = "sent"
                db.commit()
        finally:
            db.close()
    except Exception as exc:
        db = SessionLocal()
        try:
            record = db.get(WechatMessage, record_id)
            if record:
                record.status = "failed"
                record.error = f"{type(exc).__name__}: {str(exc)[:300]}"
                db.commit()
        finally:
            db.close()
        logger.exception("微信公众号异步回复失败 record_id=%s", record_id)


def _callback_configuration() -> str:
    if os.getenv("WECHAT_ENABLED", "false").strip().lower() != "true":
        raise HTTPException(status_code=503, detail="微信公众号通道尚未启用")
    if not _wechat_token():
        raise HTTPException(status_code=503, detail="尚未配置 WECHAT_TOKEN")
    try:
        mode = encryption_mode()
        if mode != "plain":
            WechatCrypto.from_env()
        return mode
    except WechatCryptoError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


def _reply_response(content: bytes, encrypted: bool, nonce: str) -> Response:
    if encrypted:
        content = WechatCrypto.from_env().reply(content, nonce)
    return Response(content=content, media_type="application/xml")


@router.get("/callback", response_class=PlainTextResponse, summary="验证微信公众号回调地址")
def verify_callback(
    signature: str = Query(default=""),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
    msg_signature: str = Query(default=""),
):
    mode = _callback_configuration()
    if msg_signature:
        if mode == "plain":
            raise HTTPException(status_code=403, detail="当前配置不接受加密验证")
        crypto = WechatCrypto.from_env()
        if not crypto.verify(msg_signature, echostr, timestamp, nonce):
            raise HTTPException(status_code=403, detail="微信加密签名校验失败")
        try:
            return crypto.decrypt(echostr).decode("utf-8")
        except (WechatCryptoError, UnicodeError):
            raise HTTPException(status_code=400, detail="加密验证消息无效") from None
    # 公众号 URL 初次校验可使用普通 echostr；安全模式仅允许此 GET，不允许明文 POST。
    if not verify_wechat_signature(signature, timestamp, nonce):
        raise HTTPException(status_code=403, detail="微信签名校验失败")
    return echostr


@router.post("/callback", summary="接收微信公众号消息")
async def receive_message(
    request: Request,
    background_tasks: BackgroundTasks,
    signature: str = Query(default=""),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    msg_signature: str = Query(default=""),
):
    mode = _callback_configuration()
    try:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 256 * 1024:
                raise HTTPException(status_code=413, detail="微信消息体过大")
        payload = parse_wechat_xml(bytes(body))
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("微信公众号 XML 解析失败：%s", type(exc).__name__)
        raise HTTPException(status_code=400, detail="微信消息格式无效") from exc

    encrypted = bool(payload.get("Encrypt"))
    if encrypted:
        if mode == "plain":
            raise HTTPException(status_code=403, detail="当前配置不接受加密消息")
        crypto = WechatCrypto.from_env()
        if not crypto.verify(msg_signature, payload["Encrypt"], timestamp, nonce):
            raise HTTPException(status_code=403, detail="微信加密签名校验失败")
        try:
            outer_to_user = payload.get("ToUserName")
            payload = parse_wechat_xml(crypto.decrypt(payload["Encrypt"]))
            if outer_to_user and outer_to_user != payload.get("ToUserName"):
                raise ValueError("收件方不一致")
        except (ValueError, WechatCryptoError):
            raise HTTPException(status_code=400, detail="微信加密消息无效") from None
    else:
        if mode == "safe" or request.query_params.get("encrypt_type") == "aes" or msg_signature:
            raise HTTPException(status_code=403, detail="当前请求必须使用加密消息")
        if not verify_wechat_signature(signature, timestamp, nonce):
            raise HTTPException(status_code=403, detail="微信签名校验失败")

    from_user = payload.get("FromUserName", "").strip()
    to_user = payload.get("ToUserName", "").strip()
    message_type = payload.get("MsgType", "").strip().lower()
    if not from_user or not to_user or not message_type:
        raise HTTPException(status_code=400, detail="微信消息缺少必要字段")
    original_id = os.getenv("WECHAT_ORIGINAL_ID", "").strip()
    if original_id and to_user != original_id:
        raise HTTPException(status_code=403, detail="消息不属于当前公众号")

    if message_type != "event" and _reply_mode() == "async" and not wechat_adapter.configured:
        raise HTTPException(status_code=503, detail="异步回复需要配置 WECHAT_APP_ID 和 WECHAT_APP_SECRET")

    external_id = _external_message_id(payload)
    db = SessionLocal()
    try:
        existing = db.scalar(
            select(WechatMessage).where(WechatMessage.external_message_id == external_id)
        )
        if existing:
            if _reply_mode() == "passive" and existing.reply_content and not _wants_schedule_page(existing.content):
                return _reply_response(build_text_reply(from_user, to_user, existing.reply_content), encrypted, nonce)
            return PlainTextResponse("success")

        record = WechatMessage(
            external_message_id=external_id,
            app_id=app_id() or None,
            from_user=from_user,
            to_user=to_user,
            message_type=message_type,
            content=payload.get("Content", "").strip(),
            status="received",
        )
        db.add(record)
        db.flush()
        note_inbound(db, payload, app_id())
        if message_type == "event":
            handle_event(db, payload, app_id())
            record.status = "event_processed"
            record.processed_at = datetime.now()
        db.commit()
        db.refresh(record)
        record_id = record.id
    except IntegrityError:
        db.rollback()
        return PlainTextResponse("success")
    except (ValueError, NotificationBlocked):
        db.rollback()
        raise HTTPException(status_code=400, detail="微信消息字段无效") from None
    finally:
        db.close()

    # 生命周期、订阅授权、通知回执均只 ACK，不启动 AI，也不消耗客服消息额度。
    if message_type == "event":
        return PlainTextResponse("success")

    base_url = public_base_url(request)
    if _reply_mode() == "async":
        background_tasks.add_task(_process_and_send, record_id, base_url)
        return PlainTextResponse("success")

    try:
        _, reply = _process_message(record_id, base_url)
    except Exception:
        reply = "小满暂时打了个盹，请稍后再试。"
    return _reply_response(build_text_reply(from_user, to_user, reply), encrypted, nonce)
