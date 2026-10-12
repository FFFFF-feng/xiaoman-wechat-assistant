from __future__ import annotations

import re
from datetime import datetime, time, timedelta

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .model_parser import CHAT_HISTORY_LIMIT, parse_with_model
from .models import ClawbotMessage, Message, Task, WechatDialogueMessage, WechatMessage
from .clawbot_identity import is_clawbot_owner
from .clawbot_delivery import enabled as clawbot_reminders_enabled
from .parser import (
    CREATE_PATTERN,
    RELATIVE_MINUTES_PATTERN,
    ParsedCommand,
    has_explicit_date,
    has_time_expression,
    now_local,
    parse_command,
    parse_time_followup,
    reminder_intent_guard,
)
from .schemas import ChatResponse
from .wechat_notifications import NotificationBlocked, app_id, official_mode, plan_reminder, recipient_for


def record_message(db: Session, message_type: str, content: str) -> None:
    db.add(Message(message_type=message_type, content=content, created_at=datetime.now()))


def _find_task(
    db: Session,
    title: str | None,
    include_done: bool = False,
    owner_id: str | None = None,
) -> Task | None:
    query = select(Task).order_by(desc(Task.created_at))
    if owner_id is not None:
        query = query.where(Task.owner_id == owner_id)
    else:
        query = query.where(Task.owner_id.is_(None))
    if not include_done:
        query = query.where(Task.status.notin_(["done", "cancelled"]))
    tasks = db.scalars(query).all()
    if not title:
        return tasks[0] if len(tasks) == 1 else None
    normalized = re.sub(r"\s+", "", title).lower()
    for task in tasks:
        task_title = re.sub(r"\s+", "", task.title).lower()
        if normalized in task_title or task_title in normalized:
            return task
    return None


def _recent_chat_history(db: Session, conversation_id: str | None = None) -> list[dict[str, str]]:
    if CHAT_HISTORY_LIMIT <= 0:
        return []
    if is_clawbot_owner(conversation_id):
        rows = db.scalars(
            select(ClawbotMessage)
            .where(ClawbotMessage.owner_id == conversation_id,
                   ClawbotMessage.status == "processed", ClawbotMessage.reply_content.is_not(None))
            .order_by(desc(ClawbotMessage.created_at), desc(ClawbotMessage.id))
            .limit(CHAT_HISTORY_LIMIT)
        ).all()
        return [item for row in reversed(rows) for item in (
            {"role": "user", "content": row.content},
            {"role": "assistant", "content": row.reply_content or ""},
        )]
    if conversation_id and conversation_id.startswith("wechat-dialogue:"):
        user_id = conversation_id.partition(":")[2]
        rows = db.scalars(
            select(WechatDialogueMessage)
            .where(
                WechatDialogueMessage.user_id == user_id,
                WechatDialogueMessage.reply_content.is_not(None),
            )
            .order_by(desc(WechatDialogueMessage.created_at), desc(WechatDialogueMessage.id))
            .limit(CHAT_HISTORY_LIMIT)
        ).all()
        rows.reverse()
        history: list[dict[str, str]] = []
        for row in rows:
            if row.content:
                history.append({"role": "user", "content": row.content})
            if row.reply_content:
                history.append({"role": "assistant", "content": row.reply_content})
        return history
    if conversation_id and conversation_id.startswith("wechat:"):
        from_user = recipient_for(conversation_id)
        query = select(WechatMessage).where(
                WechatMessage.from_user == from_user,
                WechatMessage.reply_content.is_not(None),
            )
        if official_mode():
            query = query.where(WechatMessage.app_id == app_id())
        rows = db.scalars(
            query
            .order_by(desc(WechatMessage.created_at), desc(WechatMessage.id))
            .limit(CHAT_HISTORY_LIMIT)
        ).all()
        rows.reverse()
        history: list[dict[str, str]] = []
        for row in rows:
            if row.content:
                history.append({"role": "user", "content": row.content})
            if row.reply_content:
                history.append({"role": "assistant", "content": row.reply_content})
        return history
    rows = db.scalars(
        select(Message)
        .where(Message.message_type.in_(["chat_user", "chat_assistant"]))
        .order_by(desc(Message.created_at), desc(Message.id))
        .limit(CHAT_HISTORY_LIMIT * 2)
    ).all()
    rows.reverse()
    return [
        {
            "role": "user" if row.message_type == "chat_user" else "assistant",
            "content": row.content,
        }
        for row in rows
    ]


def _latest_user_text(history: list[dict[str, str]]) -> str | None:
    for item in reversed(history):
        if item.get("role") == "user" and item.get("content", "").strip():
            return item["content"].strip()
    return None


def _latest_assistant_text(history: list[dict[str, str]]) -> str | None:
    for item in reversed(history):
        if item.get("role") == "assistant" and item.get("content", "").strip():
            return item["content"].strip()
    return None


def _reconcile_commands(
    text: str,
    model_command: ParsedCommand | None,
    rule_command: ParsedCommand,
    followup_command: ParsedCommand | None,
) -> ParsedCommand:
    """模型优先决定意图；本地规则只负责校验和标准化任务参数。

    模型成功返回时，只有它明确给出任务 action 才能进入任务分支。
    创建和改期的日期时间仍由本地规则复核，避免模型幻觉写入数据库。
    """
    guarded = reminder_intent_guard(text)
    if guarded is not None:
        # 保留模型的安全聊天/澄清，但错误的任务 action 不能越过规则边界。
        if model_command is not None and model_command.action in {"chat", "clarify"}:
            return model_command
        guarded.source = "hybrid" if model_command is not None else "rules"
        return guarded
    if followup_command is not None:
        if followup_command.action == "create" and model_command and model_command.title:
            followup_command.title = model_command.title
        followup_command.source = "hybrid" if model_command is not None else "rules"
        return followup_command

    # 已过的“今天几点”不能被模型悄悄改成明天。
    if (
        model_command is not None
        and model_command.action in {"create", "snooze", "clarify"}
        and rule_command.action == "clarify"
        and "今天这个时间已经过去了" in (rule_command.message or "")
    ):
        rule_command.source = "hybrid"
        return rule_command

    # 模型若仍沿用旧规则追问“哪一天”，以已验证的本地解析为准。
    # 条件提醒仍由模型拒绝，避免“如果下雨就提醒我”被误创建。
    if (
        model_command is not None
        and model_command.action == "clarify"
        and rule_command.action in {"create", "snooze"}
        and not has_explicit_date(text)
        and re.search(r"日期|哪天|哪一天|几号|今天|明天", model_command.message or "")
        and not re.search(r"如果|假如|要是|一旦", text)
    ):
        rule_command.source = "hybrid"
        return rule_command

    # “两分钟后”可由本地时钟精确计算。模型偶尔把它误当作模糊时间，
    # 此时只覆盖模型的追问；模型判断为普通聊天时仍以模型意图为准。
    if (
        model_command is not None
        and model_command.action == "clarify"
        and rule_command.action == "create"
        and rule_command.due_at is not None
        and re.search(RELATIVE_MINUTES_PATTERN, text)
    ):
        rule_command.source = "hybrid"
        return rule_command

    if model_command is not None:
        if model_command.action in {
            "chat", "clarify", "unknown", "list_today", "list_open", "complete", "cancel"
        }:
            return model_command
        if model_command.action in {"create", "snooze"}:
            if rule_command.action == model_command.action and rule_command.due_at:
                return ParsedCommand(
                    action=model_command.action,
                    title=model_command.title or rule_command.title,
                    due_at=rule_command.due_at,
                    confidence=model_command.confidence,
                    message=model_command.message,
                    source="hybrid",
                )
            if rule_command.action == "clarify" and re.search(
                rf"(?:{CREATE_PATTERN}|延后|推迟|延期|改到)", text
            ):
                rule_command.source = "hybrid"
                return rule_command
            if not has_explicit_date(text):
                if not has_time_expression(text):
                    return ParsedCommand(
                        action="clarify",
                        confidence="low",
                        message="请告诉我具体几点；不写日期默认今天。",
                        source="hybrid",
                    )
                if model_command.due_at is None or model_command.due_at.date() != now_local().date():
                    return ParsedCommand(
                        action="clarify",
                        confidence="low",
                        message="没有写日期时我只按今天安排；如果想定在明天，请明确说“明天”。",
                        source="hybrid",
                    )
            if model_command.due_at is None or model_command.due_at <= now_local():
                return ParsedCommand(
                    action="clarify",
                    confidence="low",
                    message="这个时间已经过去了，请换一个之后的日期和时间。",
                    source="hybrid",
                )
            return ParsedCommand(
                action=model_command.action,
                title=model_command.title,
                due_at=model_command.due_at,
                confidence=model_command.confidence,
                message=model_command.message,
                source="model",
            )

    if rule_command.action in {"create", "snooze"}:
        rule_command.source = "hybrid" if model_command is not None else "rules"
        return rule_command

    if rule_command.action == "clarify" and re.search(
        rf"(?:{CREATE_PATTERN}|延后|推迟|延期|改到)", text
    ):
        rule_command.source = "hybrid" if model_command is not None else "rules"
        return rule_command

    return model_command or rule_command


def _execute_command(
    command: ParsedCommand,
    db: Session,
    owner_id: str | None = None,
) -> ChatResponse:
    if command.action == "chat":
        return ChatResponse(
            reply=command.message or "我在听呢。你愿意再多说一点吗？",
            action="chat",
        )
    if command.action == "clarify":
        return ChatResponse(reply=command.message or "请补充时间。", action="clarify")
    if command.action == "unknown":
        return ChatResponse(reply=command.message or "我还没听懂。", action="unknown")
    if command.action == "create":
        try:
            channel, template_id = plan_reminder(db, owner_id, command.due_at or now_local(), command.title or "提醒")
        except NotificationBlocked as exc:
            return ChatResponse(reply=str(exc), action="clarify")
        task = Task(
            title=command.title or "未命名任务",
            due_at=command.due_at or now_local(),
            status="open",
            owner_id=owner_id,
            wechat_app_id=(app_id() or None) if owner_id and owner_id.startswith("wechat:") else None,
            reminder_channel=channel,
            notification_template_id=template_id,
        )
        db.add(task)
        db.flush()
        if owner_id is None:
            record_message(db, "user", f"创建：{task.title}")
        db.commit()
        db.refresh(task)
        return ChatResponse(
            reply=(f"已记录：{task.title}，时间 {task.due_at.strftime('%m月%d日 %H:%M')}。"
                   + ("\n到时会尝试通过微信提醒，请保持电脑、小满和微信网关运行并联网。" if clawbot_reminders_enabled()
                      else "\n微信插件的主动提醒尚未接通，暂不会自动发送。")) if is_clawbot_owner(owner_id)
                  else f"收到，我会在 {task.due_at.strftime('%m月%d日 %H:%M')} 提醒你：{task.title}。",
            action="create",
            task=task,
        )
    if command.action in {"list_today", "list_open"}:
        query = select(Task).order_by(Task.due_at.asc())
        if owner_id is not None:
            query = query.where(Task.owner_id == owner_id)
        else:
            query = query.where(Task.owner_id.is_(None))
        if command.action == "list_today":
            start = datetime.combine(now_local().date(), time.min)
            query = query.where(Task.due_at >= start, Task.due_at < start + timedelta(days=1))
        else:
            query = query.where(Task.status.in_(["open", "snoozed"]))
        tasks = db.scalars(query).all()
        if not tasks:
            return ChatResponse(reply="现在没有符合条件的任务，猫爪暂时空空的。", action=command.action)
        lines = [f"{i + 1}. {task.title}（{task.due_at.strftime('%m月%d日 %H:%M')}，{task.status}）" for i, task in enumerate(tasks)]
        return ChatResponse(reply="给你列好啦：\n" + "\n".join(lines), action=command.action)

    task = _find_task(db, command.title, owner_id=owner_id)
    if not task:
        return ChatResponse(
            reply=f"我没找到“{command.title or '这个'}”对应的未完成任务，可以换个关键词试试。",
            action=command.action,
        )
    if command.action == "complete":
        task.status = "done"
        reply = f"好耶，已完成「{task.title}」！"
    elif command.action == "cancel":
        task.status = "cancelled"
        reply = f"已取消「{task.title}」。"
    elif command.action == "snooze":
        try:
            channel, template_id = plan_reminder(db, owner_id, command.due_at or task.due_at,
                                                 task.title, exclude_task_id=task.id)
        except NotificationBlocked as exc:
            return ChatResponse(reply=str(exc), action="clarify")
        task.due_at = command.due_at or task.due_at
        task.reminder_channel = channel
        task.notification_template_id = template_id
        task.status = "snoozed"
        reply = f"知道啦，「{task.title}」延后到 {task.due_at.strftime('%m月%d日 %H:%M')}。"
        if is_clawbot_owner(owner_id):
            reply += ("\n到时会尝试通过微信提醒，请保持本机服务运行并联网。" if clawbot_reminders_enabled()
                      else "\n微信插件的主动提醒尚未接通，暂不会自动发送。")
    else:
        reply = "已处理。"
    task.updated_at = datetime.now()
    if owner_id is None:
        record_message(db, "user", reply)
    db.commit()
    db.refresh(task)
    return ChatResponse(reply=reply, action=command.action, task=task)


def handle_chat_message(
    content: str,
    db: Session,
    conversation_id: str | None = None,
    use_model: bool = True,
) -> ChatResponse:
    """网页与微信共用的聊天入口。调用方负责异常回滚。"""
    normalized = content.strip()
    if not normalized:
        return ChatResponse(reply="你可以发一句话给我。", action="clarify")

    history = _recent_chat_history(db, conversation_id=conversation_id)
    followup_command = parse_time_followup(
        normalized,
        _latest_user_text(history),
        _latest_assistant_text(history),
    )
    rule_command = parse_command(normalized)
    model_command = parse_with_model(normalized, history=history) if use_model else None
    command = _reconcile_commands(normalized, model_command, rule_command, followup_command)
    if use_model and model_command is None and command.action == "unknown":
        command = ParsedCommand(
            action="chat",
            confidence="low",
            message="我想认真陪你聊，不过大模型现在没有连接成功。你可以稍后再试，提醒和任务功能仍然可以使用。",
            source="rules",
        )

    remote_conversation = bool(conversation_id and conversation_id.startswith("wechat")) or is_clawbot_owner(conversation_id)
    owner_id = conversation_id if remote_conversation else None
    response = _execute_command(command, db, owner_id=owner_id)
    response.parser = command.source
    if not remote_conversation:
        record_message(db, "chat_user", normalized)
        record_message(db, "chat_assistant", response.reply)
    db.commit()
    return response
