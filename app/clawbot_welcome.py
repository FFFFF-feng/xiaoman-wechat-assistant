"""A separate durable onboarding outbox, not a fake reminder/task."""
from datetime import timedelta
import hashlib
import hmac
import secrets
from fastapi import HTTPException
from sqlalchemy import select, update
from .clawbot_identity import clawbot_owner
from .models import ClawbotWelcome
from .schedule_access import utc_now

CLAIM_LIFETIME = timedelta(seconds=45)
UNCERTAIN_AFTER = timedelta(seconds=120)


def register(db, owner):
    # New enrollments only; never blast legacy users after an upgrade.
    if not db.scalar(select(ClawbotWelcome).where(ClawbotWelcome.owner_id == owner)):
        db.add(ClawbotWelcome(owner_id=owner, status="waiting"))


def activate(db, owner):
    # Authenticated inbound content establishes Tencent's reply context.
    db.execute(update(ClawbotWelcome).where(ClawbotWelcome.owner_id == owner,
        ClawbotWelcome.status == "waiting").values(status="pending"))
    db.commit()


def guide_reply(base_url):
    if not base_url:
        return "引导网页暂时不可用。你可以在 ClawBot 设置中手动修改头像和备注名为“小满”。先试试“查询待办”；网页恢复后再发“使用指南”。"
    return f"[点击查看小满使用指南]({base_url}/guide)\n教你设置头像和“小满”备注名，也能复制聊天、语音和提醒的体验示例。网页不会自动修改微信资料。"


def recover(db, now):
    for item in db.scalars(select(ClawbotWelcome).where(ClawbotWelcome.status.in_(["claimed", "sending"]))):
        if item.status == "claimed" and item.claimed_at and now - item.claimed_at > CLAIM_LIFETIME:
            item.status, item.claim_hash = "pending", None
        elif item.status == "sending" and item.started_at and now - item.started_at > UNCERTAIN_AFTER:
            item.status, item.error_code = "unknown", "send_result_unknown"
    db.commit()


def claim(db, bindings, base_url):
    now = utc_now()
    recover(db, now)
    if not base_url:
        return None
    mapping = {clawbot_owner(v["account_id"], v["sender_id"]): v for v in bindings}
    for item in db.scalars(select(ClawbotWelcome).where(ClawbotWelcome.status == "pending")
                           .order_by(ClawbotWelcome.id).limit(200)):
        binding = mapping.get(item.owner_id)
        if not binding:
            item.status, item.error_code = "blocked", "binding_removed"
            db.commit()
            continue
        token = secrets.token_hex(32)
        changed = db.execute(update(ClawbotWelcome).where(ClawbotWelcome.id == item.id,
            ClawbotWelcome.status == "pending").values(status="claimed", claimed_at=now,
            claim_hash=hashlib.sha256(token.encode()).hexdigest()))
        db.commit()
        if changed.rowcount == 1:
            return {"id": item.id, "claim_token": token, **binding,
                    "text": "你好，我是小满。先花一分钟认识我吧：\n" + guide_reply(base_url)}
    return None


def leased(db, item_id, token):
    item = db.get(ClawbotWelcome, item_id)
    if not item or not item.claim_hash or not hmac.compare_digest(
            item.claim_hash, hashlib.sha256(token.encode()).hexdigest()):
        raise HTTPException(409, "欢迎消息领取凭据无效")
    return item


def begin_send(db, item_id, token, bindings):
    item = leased(db, item_id, token)
    now = utc_now()
    if item.status != "claimed" or not item.claimed_at or now - item.claimed_at > CLAIM_LIFETIME:
        raise HTTPException(409, "欢迎消息领取已失效")
    if item.owner_id not in {clawbot_owner(v["account_id"], v["sender_id"]) for v in bindings}:
        item.status, item.error_code = "blocked", "binding_removed"
        db.commit()
        raise HTTPException(409, "接入已撤销")
    changed = db.execute(update(ClawbotWelcome).where(ClawbotWelcome.id == item.id,
        ClawbotWelcome.status == "claimed", ClawbotWelcome.claim_hash == item.claim_hash)
        .values(status="sending", started_at=now))
    db.commit()
    if changed.rowcount != 1:
        raise HTTPException(409, "欢迎消息发送已经开始")


def complete_send(db, item_id, token, outcome, message_id, error_code):
    item = leased(db, item_id, token)
    if item.status == outcome:
        return
    if item.status not in {"sending", "unknown"}:
        raise HTTPException(409, "欢迎消息状态不允许更新")
    if outcome == "accepted" and not message_id:
        raise HTTPException(422, "平台受理须有消息标识")
    item.status, item.external_message_id, item.error_code = outcome, message_id, error_code
    db.commit()
