"""正式公众号的通知资格检查、一次性订阅额度和送达回执。"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from xml.etree.ElementTree import fromstring
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .adapters.wechat_official import wechat_adapter
from .models import Reminder, Task, WechatDeliveryReceipt, WechatSubscription, WechatUser


class NotificationBlocked(RuntimeError):
    def __init__(self, message: str, code: str = "notification_blocked"):
        self.code = code
        super().__init__(message)


def local_now() -> datetime:
    return datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Asia/Shanghai"))).replace(tzinfo=None)


def app_id() -> str:
    return os.getenv("WECHAT_APP_ID", "").strip()


def official_mode() -> bool:
    mode = os.getenv("WECHAT_ACCOUNT_MODE", "test").strip().lower()
    if mode not in {"test", "official"}:
        raise NotificationBlocked("WECHAT_ACCOUNT_MODE 必须为 test 或 official", "configuration")
    return mode == "official"


def owner_for(openid: str, account: str | None = None) -> str:
    account = account if account is not None else app_id()
    return f"wechat:{account}:{openid}" if official_mode() else f"wechat:{openid}"


def recipient_for(owner_id: str) -> str:
    return owner_id.rsplit(":", 1)[-1]


def reminder_mode() -> str:
    mode = os.getenv("WECHAT_REMINDER_MODE", "customer").strip().lower()
    if mode not in {"customer", "template", "subscribe"}:
        raise NotificationBlocked("WECHAT_REMINDER_MODE 必须为 customer、template 或 subscribe", "configuration")
    return mode


def template_config(channel: str) -> tuple[str, dict]:
    prefix = "WECHAT_TEMPLATE" if channel == "template" else "WECHAT_SUBSCRIBE"
    template_id = os.getenv(prefix + "_ID", "").strip()
    try:
        fields = json.loads(os.getenv(prefix + "_DATA", ""))
        if not template_id or not isinstance(fields, dict) or not 1 <= len(fields) <= 20:
            raise ValueError
        for key, spec in fields.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", key):
                raise ValueError
            if not isinstance(spec, dict) or not isinstance(spec.get("value"), str):
                raise ValueError
            if set(spec) - {"value", "color"}:
                raise ValueError
            if "color" in spec and not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(spec["color"])):
                raise ValueError
            rendered = spec["value"]
            for placeholder in ("title", "title_short", "due_at", "task_id"):
                rendered = rendered.replace("{" + placeholder + "}", "")
            if "{" in rendered or "}" in rendered:
                raise ValueError
        return template_id, fields
    except (ValueError, TypeError) as exc:
        raise NotificationBlocked(f"{prefix}_ID / {prefix}_DATA 未配置或格式无效，请按账号的真实模板配置", "configuration") from exc


def notification_data(task: Task, channel: str) -> dict:
    template_id, fields = template_config(channel)
    if task.notification_template_id and task.notification_template_id != template_id:
        raise NotificationBlocked("任务的通知模板与当前配置不一致，请重新确认提醒", "template_changed")
    values = {"title": task.title, "title_short": task.title[:20],
              "due_at": task.due_at.strftime("%Y-%m-%d %H:%M"), "task_id": str(task.id or "")}
    result = {}
    for key, spec in fields.items():
        value = spec["value"]
        for placeholder, replacement in values.items():
            value = value.replace("{" + placeholder + "}", replacement)
        # 按当前官方模板字段类型限长。使用 title_short 时才显式截断任务标题。
        limit = next((limit for prefix, limit in (("thing", 20), ("name", 10), ("phrase", 5),
                                                 ("character_string", 32), ("letter", 32))
                      if re.fullmatch(prefix + r"\d+", key)), None)
        if limit and len(value) > limit:
            raise NotificationBlocked(f"通知字段 {key} 超过 {limit} 字符，请调整模板映射", "template_data")
        result[key] = {"value": value}
        if channel == "template" and "color" in spec:
            result[key]["color"] = spec["color"]
    return result


def notification_config_status() -> dict:
    errors = []
    mode = "invalid"
    account = "invalid"
    try:
        account = "official" if official_mode() else "test"
        mode = reminder_mode()
        if mode != "customer":
            template_config(mode)
    except NotificationBlocked as exc:
        errors.append(str(exc))
    return {"mode": mode, "account_mode": account, "configuration_errors": errors,
            "customer_window_hours": 48, "customer_message_limit": 5,
            "subscription_requires_user_consent": True,
            "permission_verified": False}


def user_state(db: Session, account: str, openid: str) -> WechatUser | None:
    return db.scalar(select(WechatUser).where(WechatUser.app_id == account, WechatUser.openid == openid))


def note_inbound(db: Session, payload: dict[str, str], account: str, now: datetime | None = None) -> None:
    """只有用户真正发送消息才启动 48h 窗口；送达、订阅、菜单事件均不延长它。"""
    now = now or local_now()
    openid = payload["FromUserName"]
    state = user_state(db, account, openid)
    if state is None:
        state = WechatUser(app_id=account, openid=openid, active=True, customer_messages_used=0)
        db.add(state)
        db.flush()
    msg_type = payload.get("MsgType", "").lower()
    event = payload.get("Event", "").lower()
    if event == "unsubscribe":
        state.active = False
        state.last_message_at = None
        state.customer_messages_used = 0
        db.execute(update(WechatSubscription).where(WechatSubscription.app_id == account,
                                                   WechatSubscription.openid == openid).values(credits=0))
    elif event == "subscribe":
        state.active = True
    if msg_type in {"text", "image", "voice", "video", "shortvideo", "location", "link"}:
        try:
            message_at = datetime.fromtimestamp(int(payload["CreateTime"]),
                                              ZoneInfo(os.getenv("TIMEZONE", "Asia/Shanghai"))).replace(tzinfo=None)
        except (KeyError, ValueError, OverflowError, OSError):
            raise NotificationBlocked("消息 CreateTime 无效", "invalid_message") from None
        message_at = min(message_at, now)
        if state.last_message_at is None or message_at > state.last_message_at:
            state.last_message_at = message_at
            state.customer_messages_used = 0
            state.active = True


def _pending_count(db: Session, owner_id: str, channel: str, exclude_task_id: int | None = None,
                   template_id: str | None = None) -> int:
    query = select(func.count(Task.id)).where(
        Task.owner_id == owner_id, Task.wechat_app_id == app_id(),
        Task.status.in_(["open", "snoozed"]), Task.reminder_channel == channel,
        ~select(Reminder.id).where(Reminder.task_id == Task.id, Reminder.reminder_time == Task.due_at).exists(),
    )
    if exclude_task_id is not None:
        query = query.where(Task.id != exclude_task_id)
    if template_id is not None:
        query = query.where(Task.notification_template_id == template_id)
    return db.scalar(query) or 0


def plan_reminder(db: Session, owner_id: str | None, due_at: datetime, title: str = "提醒",
                  exclude_task_id: int | None = None, reserve_reply: bool = True) -> tuple[str | None, str | None]:
    if not owner_id or not owner_id.startswith("wechat:"):
        return None, None
    if not official_mode() and reminder_mode() == "customer":
        return "customer", None
    if not app_id() or owner_id != owner_for(recipient_for(owner_id)):
        raise NotificationBlocked("这条日程属于其他微信账号，请在当前正式号重新创建", "account_mismatch")
    if os.getenv("WECHAT_ENABLED", "false").strip().lower() != "true" or not wechat_adapter.configured:
        raise NotificationBlocked("正式公众号发送参数尚未配置，暂时不能创建微信提醒", "configuration")
    state = user_state(db, app_id(), recipient_for(owner_id))
    if state is None or not state.active:
        raise NotificationBlocked("请先关注公众号并发送一条消息，再创建提醒", "no_interaction")
    mode = reminder_mode()
    if mode == "customer":
        if state.last_message_at is None or due_at >= state.last_message_at + timedelta(hours=48):
            raise NotificationBlocked("这个提醒超出了微信客服消息的48小时窗口。请改到窗口内，或由管理员开通并配置模板/订阅通知后再创建。", "customer_window")
        pending = _pending_count(db, owner_id, mode, exclude_task_id)
        reply_mode = os.getenv("WECHAT_REPLY_MODE", "auto").strip().lower()
        reply_cost = int(reserve_reply and reply_mode != "passive")
        if state.customer_messages_used + pending + reply_cost + 1 > 5:
            raise NotificationBlocked("微信客服消息额度不足以安排更多提醒。请先处理已有提醒，或使用已开通的模板/订阅通知。", "customer_quota")
        return mode, None
    template_id, _ = template_config(mode)
    # 在保存前也校验真实标题能否填入模板，避免等到提醒触发才发现字段超长。
    notification_data(Task(title=title, due_at=due_at, notification_template_id=template_id), mode)
    if mode == "subscribe":
        grant = db.scalar(select(WechatSubscription).where(
            WechatSubscription.app_id == app_id(), WechatSubscription.openid == recipient_for(owner_id),
            WechatSubscription.template_id == template_id))
        pending = _pending_count(db, owner_id, mode, exclude_task_id, template_id)
        if grant is None or grant.credits <= pending:
            raise NotificationBlocked("还没有这条提醒可用的订阅授权。请先在公众号的订阅通知入口同意订阅，再重新创建；一次同意只能对应一次通知。", "subscription_consent")
    return mode, template_id


def send_customer(db: Session, recipient: str, content: str, now: datetime | None = None) -> bool:
    if official_mode():
        now = now or local_now()
        # 先占额度，再请求微信；超时不盲目重试，以免重复回复。
        result = db.execute(update(WechatUser).where(
            WechatUser.app_id == app_id(), WechatUser.openid == recipient, WechatUser.active.is_(True),
            WechatUser.last_message_at > now - timedelta(hours=48), WechatUser.customer_messages_used < 5,
        ).values(customer_messages_used=WechatUser.customer_messages_used + 1))
        db.commit()
        if result.rowcount != 1:
            raise NotificationBlocked("微信客服消息窗口已关闭或发送额度不足", "customer_window_or_quota")
    return wechat_adapter.send(recipient, content)


def send_reminder(db: Session, task: Task, content: str, now: datetime) -> tuple[str, dict]:
    if os.getenv("WECHAT_ENABLED", "false").strip().lower() != "true" or not wechat_adapter.configured:
        raise NotificationBlocked("微信公众号发送参数尚未启用或配置", "configuration")
    if (task.wechat_app_id and task.wechat_app_id != app_id()) or (official_mode() and
            (task.wechat_app_id != app_id() or task.owner_id != owner_for(recipient_for(task.owner_id or "")))):
        raise NotificationBlocked("任务未绑定当前公众号；不会把测试号用户的任务发送到正式号", "account_mismatch")
    state = user_state(db, app_id(), recipient_for(task.owner_id or ""))
    if state is not None and not state.active:
        raise NotificationBlocked("用户已经取消关注", "unsubscribed")
    channel = task.reminder_channel or reminder_mode()
    recipient = recipient_for(task.owner_id or "")
    if channel == "customer":
        send_customer(db, recipient, content, now)
        return channel, {"errcode": 0}
    data = notification_data(task, channel)
    template_id = task.notification_template_id or template_config(channel)[0]
    if channel == "subscribe":
        result = db.execute(update(WechatSubscription).where(
            WechatSubscription.app_id == app_id(), WechatSubscription.openid == recipient,
            WechatSubscription.template_id == template_id, WechatSubscription.credits > 0,
        ).values(credits=WechatSubscription.credits - 1))
        db.commit()
        if result.rowcount != 1:
            raise NotificationBlocked("订阅授权不足或已被用户取消", "subscription_consent")
    return channel, wechat_adapter.send_notification(channel, recipient, template_id, data)


def apply_receipt(db: Session, reminder: Reminder, account: str) -> None:
    if not reminder.external_message_id:
        return
    receipt = db.scalar(select(WechatDeliveryReceipt).where(
        WechatDeliveryReceipt.app_id == account, WechatDeliveryReceipt.channel == reminder.channel,
        WechatDeliveryReceipt.external_message_id == reminder.external_message_id))
    if receipt:
        reminder.status = receipt.status
        reminder.error = receipt.error
        if receipt.status == "delivered":
            reminder.sent_at = receipt.created_at


def handle_event(db: Session, payload: dict[str, str], account: str) -> None:
    event = payload.get("Event", "").lower()
    entries = []
    for container in ("SubscribeMsgPopupEvent", "SubscribeMsgChangeEvent", "SubscribeMsgSentEvent"):
        if payload.get(container):
            entries.extend({child.tag: child.text or "" for child in item}
                           for item in fromstring(payload[container]).findall("List"))
    if event in {"subscribe_msg_popup_event", "subscribe_msg_change_event"}:
        try:
            event_at = datetime.fromtimestamp(int(payload["CreateTime"]),
                                             ZoneInfo(os.getenv("TIMEZONE", "Asia/Shanghai"))).replace(tzinfo=None)
        except (KeyError, ValueError, OverflowError, OSError):
            raise NotificationBlocked("订阅事件时间无效", "invalid_message") from None
        seen = set()
        for item in entries:
            template_id = item.get("TemplateId", "")
            if not template_id or template_id in seen:
                continue
            seen.add(template_id)
            grant = db.scalar(select(WechatSubscription).where(
                WechatSubscription.app_id == account, WechatSubscription.openid == payload["FromUserName"],
                WechatSubscription.template_id == template_id))
            if grant is None:
                grant = WechatSubscription(app_id=account, openid=payload["FromUserName"], template_id=template_id,
                                           credits=0, last_event_rejected=False)
                db.add(grant)
            if grant.last_event_at and (event_at < grant.last_event_at or
                    (event_at == grant.last_event_at and grant.last_event_rejected)):
                continue
            if item.get("SubscribeStatusString") == "reject":
                grant.credits = 0
                grant.last_event_rejected = True
            elif event == "subscribe_msg_popup_event" and item.get("SubscribeStatusString") == "accept":
                grant.credits += 1
                grant.last_event_rejected = False
            else:
                continue
            grant.last_event_at = event_at
            grant.updated_at = local_now()
    receipts = []
    if event == "templatesendjobfinish":
        status = payload.get("Status", "")
        receipts.append(("template", payload.get("MsgID", ""), status == "success", status))
    elif event == "subscribe_msg_sent_event":
        receipts.extend(("subscribe", item.get("MsgID", ""), item.get("ErrorCode") == "0",
                         item.get("ErrorStatus", "")) for item in entries)
    for channel, message_id, success, detail in receipts:
        if not message_id:
            continue
        receipt = db.scalar(select(WechatDeliveryReceipt).where(
            WechatDeliveryReceipt.app_id == account, WechatDeliveryReceipt.channel == channel,
            WechatDeliveryReceipt.external_message_id == message_id))
        if receipt is None:
            receipt = WechatDeliveryReceipt(app_id=account, channel=channel, external_message_id=message_id,
                                            status="delivered" if success else "failed",
                                            error=None if success else detail[:300])
            db.add(receipt)
            db.flush()
        reminders = db.scalars(select(Reminder).join(Task).where(
            Task.wechat_app_id == account, Reminder.channel == channel,
            Reminder.external_message_id == message_id)).all()
        for reminder in reminders:
            apply_receipt(db, reminder, account)
