from __future__ import annotations

import re
import json
from datetime import datetime, time, timedelta

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .model_parser import CHAT_HISTORY_LIMIT, parse_with_model
from .models import ClawbotMessage, ConversationTaskContext, Message, Task, WechatDialogueMessage, WechatMessage
from .clawbot_identity import is_clawbot_owner
from .clawbot_delivery import enabled as clawbot_reminders_enabled
from . import task_dialogue
from . import assistant_preferences
from .reply_style import completion_reply
from .parser import (
    CREATE_PATTERN,
    RELATIVE_MINUTES_PATTERN,
    ParsedCommand,
    correction_time_text,
    is_time_correction,
    has_explicit_date,
    has_time_expression,
    now_local,
    parse_command,
    parse_task_time_correction,
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
        # Imported source statements must not become a pending reminder command
        # when a later ordinary message merely supplies a time.
        rows = [row for row in rows if not row.response_json or
                json.loads(row.response_json).get("action") not in {"material_summary", "material_error", "import_collect"}]
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

    # The noon convention is validated by the same rules as explicit clocks.
    # Model chat intent still wins; an outdated missing-time question does not.
    if rule_command.assumed_time and model_command and model_command.action in {"create", "clarify"}:
        rule_command.source = "hybrid"
        return rule_command

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
                    assumed_time=rule_command.assumed_time,
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


def _friendly_due(due_at: datetime) -> str:
    today = now_local().date()
    offset = (due_at.date() - today).days
    day = {0: "今天", 1: "明天", 2: "后天"}.get(offset)
    if day is None:
        day = due_at.strftime("%Y年%m月%d日" if due_at.year != today.year else "%m月%d日")
    return f"{day} {due_at:%H:%M}"


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
        task_dialogue.operation_succeeded(db, owner_id, 'create')
        db.commit()
        db.refresh(task)
        return ChatResponse(
            reply=f"记下啦，{_friendly_due(task.due_at)}提醒你{task.title}。"
                  + ("中午我先按12:00安排，你也可以说“改成12:30”。" if command.assumed_time else "")
                  + ("自动提醒暂不可用。" if is_clawbot_owner(owner_id) and not clawbot_reminders_enabled() else ""),
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
            return ChatResponse(reply="现在没有符合条件的任务。", action=command.action)
        lines = [f"{i + 1}. {task.title}（{task.due_at.strftime('%m月%d日 %H:%M')}，{task.status}）" for i, task in enumerate(tasks)]
        return ChatResponse(reply="给你列好啦：\n" + "\n".join(lines), action=command.action)

    if command.task_id is not None:
        task = db.get(Task, command.task_id)
        if task is not None and (task.owner_id != owner_id or task.status not in {"open", "snoozed"}):
            task = None
    else:
        task = _find_task(db, command.title, owner_id=owner_id)
    if not task:
        return ChatResponse(
            reply=f"我没找到“{command.title or '这个'}”对应的未完成任务，可以换个关键词试试。",
            action=command.action,
        )
    if command.action == "complete":
        task.status = "done"
        reply = completion_reply(task.title, assistant_preferences.snapshot(db, owner_id))
    elif command.action == "cancel":
        task.status = "cancelled"
        reply = f"已取消「{task.title}」。"
    elif command.action == "snooze":
        try:
            channel, template_id = (plan_reminder(db, owner_id, command.due_at or task.due_at,
                                                 task.title, exclude_task_id=task.id)
                                    if task.reminder_enabled else (None, None))
        except NotificationBlocked as exc:
            return ChatResponse(reply=str(exc), action="clarify")
        task.due_at = command.due_at or task.due_at
        task.reminder_channel = channel
        task.notification_template_id = template_id
        task.status = "snoozed"
        reply = f"好，「{task.title}」改到{_friendly_due(task.due_at)}。"
        if is_clawbot_owner(owner_id) and not clawbot_reminders_enabled():
            reply += "自动提醒暂不可用。"
    else:
        reply = "已处理。"
    task.updated_at = datetime.now()
    if owner_id is None:
        record_message(db, "user", reply)
    task_dialogue.operation_succeeded(db, owner_id, command.action)
    db.commit()
    db.refresh(task)
    return ChatResponse(reply=reply, action=command.action, task=task)


def _fragment_safety_guard(text: str) -> ParsedCommand | None:
    """A fragmented request must not execute an early instruction later withdrawn.

    The deterministic parser supports one operation. Ambiguous multiple task/time
    revisions are confirmed rather than silently executing the first fragment.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2 or not re.search(CREATE_PATTERN, text):
        return None
    for line in lines:
        guarded = reminder_intent_guard(line)
        if guarded:
            return guarded
        plain = re.sub(r"[，,。.!！?？\s]", "", line)
        if re.fullmatch(r"(?:算了)?(?:算了|取消吧|不用了|不需要了|先别|先别(?:建|创建|设置|安排)(?:了)?)", plain):
            return ParsedCommand("clarify", confidence="low", message="好，这次先不创建提醒。")
    if sum(bool(re.search(CREATE_PATTERN, line)) for line in lines) > 1:
        return ParsedCommand("clarify", confidence="low", message="我收到几个提醒要求，先不创建。请确认要记哪件事、什么时间。")
    if sum(has_time_expression(line) or "中午" in line for line in lines) > 1 or sum(has_explicit_date(line) for line in lines) > 1:
        return ParsedCommand("clarify", confidence="low", message="你提到了不同的时间，先不创建。最后确定是哪天、几点呢？")
    return None


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
    remote_conversation = bool(conversation_id and conversation_id.startswith("wechat")) or is_clawbot_owner(conversation_id)
    owner_id = conversation_id if remote_conversation else None
    owner_key = owner_id or "local"
    receipt_text = normalized
    normalized = assistant_preferences.strip_address(db, owner_id, normalized)
    preference_command = assistant_preferences.command(db, owner_id, normalized)
    context = db.get(ConversationTaskContext, owner_key)
    correction = None
    if correction_time_text(normalized) is not None:
        task = db.get(Task, context.task_id) if context and context.expires_at > datetime.now() else None
        if task and is_clawbot_owner(owner_id):
            # Materials and import controls bypass normal chat; inspect the actual
            # last receipt, including those messages, rather than filtered history.
            rows = db.scalars(select(ClawbotMessage).where(
                ClawbotMessage.owner_id == owner_id
            ).order_by(desc(ClawbotMessage.created_at), desc(ClawbotMessage.id)).limit(2)).all()
            # The caller has already persisted this incoming text as processing.
            if rows and rows[0].status == "processing" and rows[0].content.strip() == receipt_text:
                rows = rows[1:]
            last = rows[0] if rows else None
            receipt = json.loads(last.response_json) if last and last.response_json else {}
            if not last or last.status != "processed" or receipt.get("action") not in {"create", "snooze"} or (receipt.get("task") or {}).get("id") != task.id:
                task = None
        if task and task.owner_id == owner_id and task.status in {"open", "snoozed"}:
            correction = parse_task_time_correction(normalized, task.id, task.due_at)
        else:
            correction = ParsedCommand("clarify", message="想改哪条提醒？请带上任务名称和新时间，例如“把周报改到明天12:30”。")
    if re.match(r"^(?:不要|别|不用|不需要|如果|假如|要是).*(?:改成|改到|换成|换到)", normalized):
        correction = ParsedCommand("clarify", message="这次先不修改提醒。确定要改时，告诉我任务名称和新时间就好。")
    if correction is None and is_time_correction(normalized) and (
        context is not None or has_time_expression(normalized) or has_explicit_date(normalized)
        or re.search(r"提醒|任务", normalized)
    ):
        correction = ParsedCommand("clarify", message="想改到哪个时间？请给一个确定的时刻，例如“改成12:30”。")
    fragment_guard = _fragment_safety_guard(normalized)
    pending_command = (task_dialogue.continue_draft(db, owner_id, normalized, receipt_text=receipt_text)
                       if fragment_guard is None and preference_command is None else None)
    db.flush()
    guard = (preference_command or fragment_guard or pending_command or correction
             or task_dialogue.completion_reply(db, owner_id, normalized, receipt_text=receipt_text)
             or task_dialogue.orphan_time_reply(normalized))
    rule_command = parse_command(normalized)
    model_command = (parse_with_model(normalized, history=history, preferences=assistant_preferences.snapshot(db, owner_id))
                     if use_model and guard is None else None)
    command = guard or _reconcile_commands(normalized, model_command, rule_command, None)
    if command.action == 'clarify' and guard is None:
        snooze_task = None
        match = re.match(r'^(?:请)?(?:把)?(.+?)(?:延后|推迟|延期|改到)', normalized)
        if match:
            snooze_task = _find_task(db, match[1], owner_id=owner_id)
        draft_command = task_dialogue.seed_draft(db, owner_id, normalized, snooze_task)
        if draft_command is not None:
            command = draft_command
    if use_model and model_command is None and command.action == "unknown":
        command = ParsedCommand(
            action="chat",
            confidence="low",
            message="我想认真陪你聊，不过大模型现在没有连接成功。你可以稍后再试，提醒和任务功能仍然可以使用。",
            source="rules",
        )

    response = _execute_command(command, db, owner_id=owner_id)
    response.parser = command.source
    if not remote_conversation:
        record_message(db, "chat_user", receipt_text)
        record_message(db, "chat_assistant", response.reply)
    if response.action in {"create", "snooze"} and response.task is not None:
        if context is None:
            context = ConversationTaskContext(owner_key=owner_key, task_id=response.task.id,
                                              expires_at=datetime.now() + timedelta(minutes=15))
            db.add(context)
        else:
            context.task_id = response.task.id
            context.expires_at = datetime.now() + timedelta(minutes=15)
    elif context is not None:
        db.delete(context)
    task_dialogue.finish_turn(db, owner_id, normalized, response.action, receipt_text=receipt_text)
    db.commit()
    return response
