"""Owner-scoped dialogue slots. Model prose is never used as task identity."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from . import parser
from .clawbot_identity import is_clawbot_owner
from .models import ClawbotDelivery, ClawbotMessage, PendingTaskDialogue, Reminder, ReminderDialogueFocus, Task
from .parser import ParsedCommand

DRAFT_LIFETIME = timedelta(minutes=15)
FOCUS_LIFETIME = timedelta(minutes=30)
MERIDIEM = r"上午|早上|中午|下午|晚上"
CLOCK = rf"{parser.TIME_PATTERN}|{parser.CLOCK_PATTERN}|\d{{1,2}}(?:[:：]\d{{1,2}})?\s*(?:am|pm)"


def latest_receipt(db: Session, owner: str | None, current_text: str | None = None) -> ClawbotMessage | None:
    rows = db.scalars(select(ClawbotMessage).where(ClawbotMessage.owner_id == owner)
                      .order_by(desc(ClawbotMessage.id)).limit(2)).all()
    if rows and current_text is not None and rows[0].status == 'processing' and rows[0].content.strip() == current_text:
        rows = rows[1:]
    return rows[0] if rows else None


def _date(text: str) -> datetime | None:
    matches = re.findall(parser.DATE_PATTERN, text)
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError('multiple dates')
    value = matches[0]
    base = parser.now_local().replace(hour=0, minute=0)
    offsets = {'今天': 0, '明天': 1, '后天': 2, '大后天': 3}
    if value in offsets:
        return base + timedelta(days=offsets[value])
    month, day = map(int, re.findall(r'\d+', value))
    return base.replace(year=base.year + (1 if month < base.month - 6 else 0), month=month, day=day)


def _clock(text: str, previous: str = '') -> str:
    matches = list(re.finditer(CLOCK, text, re.I))
    if len(matches) > 1:
        raise ValueError('multiple clocks')
    if matches:
        value = matches[0][0]
        prior_period = re.search(MERIDIEM, previous)
        if prior_period and not re.search(rf'{MERIDIEM}|am|pm', value, re.I):
            value = prior_period[0] + value
        return value
    period = re.search(MERIDIEM, text)
    if period:
        # Noon is the sole existing convention that includes an assumed clock.
        return '中午12点' if period[0] == '中午' else period[0]
    return previous


def _time_answer(text: str) -> str | None:
    value = text.strip()
    value = re.sub(r'^(?:那就|那|就|是|在|对|嗯|改成|改到)\s*', '', value)
    value = re.sub(r'(?:可以吗|行吗|好不好)[？?。\s]*$', '', value)
    value = re.sub(r'[吧啊呀哦呢。！!，,\s]+$', '', value)
    remainder = re.sub(parser.DATE_PATTERN, '', value)
    remainder = re.sub(parser.RELATIVE_MINUTES_PATTERN, '', remainder)
    remainder = re.sub(CLOCK, '', remainder, flags=re.I)
    remainder = re.sub(MERIDIEM, '', remainder)
    if re.sub(r'\s+', '', remainder) or not value:
        return None
    return value


def orphan_time_reply(text: str) -> ParsedCommand | None:
    if _time_answer(text) is not None:
        return ParsedCommand('clarify', message='还没确定是哪件事呢。可以一起说，比如“明天下午三点提醒我喝水”。')
    return None


def draft_command(draft: PendingTaskDialogue) -> ParsedCommand:
    if not parser.has_time_expression(draft.time_text):
        return ParsedCommand('clarify', message=f'「{draft.title}」记着呢，{draft.date_at:%m月%d日}几点提醒你？')
    due, error = parser._extract_datetime(draft.time_text, reference_date=draft.date_at)
    if error or due is None:
        return ParsedCommand('clarify', message=f'「{draft.title}」还没设好。{error or "想定在几点？"}')
    return ParsedCommand(draft.action, title=draft.title, task_id=draft.task_id, due_at=due,
                         assumed_time=draft.time_text == '中午12点')


def continue_draft(db: Session, owner: str | None, text: str) -> ParsedCommand | None:
    draft = db.get(PendingTaskDialogue, owner or 'local')
    if not draft:
        return None
    previous = latest_receipt(db, owner, text) if is_clawbot_owner(owner) else None
    valid = draft.expires_at > parser.now_exact_local()
    if is_clawbot_owner(owner):
        valid = valid and bool(previous and previous.status == 'processed' and previous.id == draft.last_message_id)
    if not valid:
        db.delete(draft)
        return None
    compact = re.sub(r'[\s，,。！!]', '', text)
    if re.fullmatch(r'(?:算了)?(?:算了|不用了|取消吧|先不设了|不需要了)', compact):
        db.delete(draft)
        return ParsedCommand('chat', message='好，这条先不设了。')
    if re.fullmatch(r'嗯+|好(?:的|呀)?|对(?:的)?|行', compact):
        return draft_command(draft)
    if not re.search(parser.CREATE_PATTERN + r'|取消|删除|完成', text) and (
        len(re.findall(parser.DATE_PATTERN, text)) > 1 or len(list(re.finditer(CLOCK, text, re.I))) > 1
    ):
        return ParsedCommand('clarify', message=f'「{draft.title}」先留着，你确定是哪天、几点？')
    answer = _time_answer(text)
    if answer is None:
        db.delete(draft)
        return None
    try:
        date = _date(answer)
        clock = _clock(answer, draft.time_text)
        if re.search(parser.RELATIVE_MINUTES_PATTERN, answer):
            due, error = parser._extract_datetime(answer)
            if error or due is None:
                return ParsedCommand('clarify', message=error or '几分钟后提醒你？')
            draft.date_at, clock = due.replace(hour=0, minute=0, second=0), due.strftime('%H点%M分')
        elif date is not None:
            draft.date_at = date
        draft.time_text = clock
    except ValueError:
        return ParsedCommand('clarify', message=f'「{draft.title}」先留着，你确定是哪天、几点？')
    # A task may have been finished/cancelled from the schedule page while waiting.
    if draft.task_id is not None:
        task = db.get(Task, draft.task_id)
        if not task or task.owner_id != owner or task.status not in {'open', 'snoozed'}:
            db.delete(draft)
            return ParsedCommand('clarify', message='这条任务已经变了，告诉我现在想改哪一条吧。')
    return draft_command(draft)


def seed_draft(db: Session, owner: str | None, text: str, task: Task | None = None) -> ParsedCommand | None:
    if parser.reminder_intent_guard(text) is not None:
        return None
    command = parser.parse_command(text)
    if command.action != 'clarify':
        return None
    action = 'snooze' if task else 'create'
    if task is None and not re.search(parser.CREATE_PATTERN, text):
        return None
    # Do not turn unsupported before/after/noon phrases into a clock convention.
    if re.search(r'中午(?:前|后|左右|期间|之前|之后)|《|》', text):
        return None
    scheduled, _ = parser._default_noon_text(text)
    # A period in the task title ("看中午新闻") is not a scheduling slot.
    period_prefix = re.match(rf'^(?:请|帮我|在|提醒我|叫我|{parser.DATE_PATTERN})*(?:{MERIDIEM})', text)
    title_text = re.sub(rf'(?:{MERIDIEM})(?=提醒|叫我|记得)', '', scheduled)
    title = task.title if task else parser._extract_title(title_text)
    if title == '未命名任务' or len(title) > 300:
        return None
    try:
        date, clock = _date(text), _clock(scheduled)
        if not parser.has_time_expression(scheduled) and not period_prefix:
            clock = ''
    except ValueError:
        return None
    draft = db.get(PendingTaskDialogue, owner or 'local')
    if draft is None:
        draft = PendingTaskDialogue(owner_key=owner or 'local')
        db.add(draft)
    draft.action, draft.title, draft.task_id = action, title, task.id if task else None
    draft.date_at = date or (task.due_at.replace(hour=0, minute=0) if task else parser.now_local().replace(hour=0, minute=0))
    draft.time_text = clock
    draft.expires_at = parser.now_exact_local() + DRAFT_LIFETIME
    return draft_command(draft)


def remember_send(db: Session, item: ClawbotDelivery, now: datetime) -> None:
    """Take the inbound-message watermark at send start, not a delayed ACK."""
    last = latest_receipt(db, item.owner_id)
    focus = db.get(ReminderDialogueFocus, item.owner_id)
    if focus is None:
        focus = ReminderDialogueFocus(owner_key=item.owner_id)
        db.add(focus)
    focus.delivery_id = item.id
    focus.expires_at = now + FOCUS_LIFETIME
    focus.last_message_id = last.id if last else None


def completion_reply(db: Session, owner: str | None, text: str) -> ParsedCommand | None:
    compact = re.sub(r'[\s，,。！!~～]', '', text)
    if not re.search(parser.CREATE_PATTERN + r'|列出|查看|查询|看看|哪些|有什么|待办', text):
        if re.search(r'(?:还没|没有|尚未|未|没|快要|快|差点|差不多)(?:完成|做完|写完|搞定)', text):
            return ParsedCommand('chat', message='好，还没做完，我先不把它标成完成。')
        if re.search(r'完成|做完|写完|搞定', text) and re.search(r'如果|假如|要是|他说|她说|[“”「」《》"？?]|(?:吗|么)$', text):
            return ParsedCommand('clarify', message='你是在说进度，还是想勾掉某条任务？我先不改。')
    # Closed grammar: quotations, questions, hypothetical or negated completion
    # must never become task mutations through a keyword or a model guess.
    affirmative = bool(re.fullmatch(r'(?:我|已经|都|我已经)?(?:完成了|搞定了|做完了|写完了|弄好了|完事了)+(?:你?别催了|不用催了)?', compact))
    negative = bool(re.fullmatch(r'(?:我)?(?:还没|没|没有|未|尚未|快|快要|差点|差不多)(?:完成|做完|写完|搞定)(?:了)?(?:你?别催了)?', compact))
    if not affirmative and not negative:
        return None
    if negative:
        return ParsedCommand('chat', message='好，还没做完，我先不把它标成完成。')
    focus = db.get(ReminderDialogueFocus, owner or 'local')
    previous = latest_receipt(db, owner, text) if is_clawbot_owner(owner) else None
    valid = bool(focus and focus.expires_at > parser.now_exact_local())
    if valid and is_clawbot_owner(owner):
        valid = (previous.id if previous else None) == focus.last_message_id
    if not valid:
        return ParsedCommand('clarify', message='做完哪一件啦？告诉我任务名，我帮你勾掉。')
    item = db.get(ClawbotDelivery, focus.delivery_id)
    reminder = db.get(Reminder, item.reminder_id) if item else None
    task = db.get(Task, reminder.task_id) if reminder else None
    if not item or item.owner_id != owner or item.status != 'accepted' or not task or task.owner_id != owner or task.status not in {'open', 'snoozed'} or task.due_at != reminder.reminder_time:
        return ParsedCommand('clarify', message='我还不能确定你说的是哪件事，告诉我任务名吧。')
    recent = db.scalars(select(ClawbotDelivery).where(
        ClawbotDelivery.owner_id == owner, ClawbotDelivery.status.in_(['accepted', 'sending', 'unknown']),
        ClawbotDelivery.started_at >= parser.now_exact_local() - FOCUS_LIFETIME,
    ).join(Reminder, Reminder.id == ClawbotDelivery.reminder_id).join(Task, Task.id == Reminder.task_id)
        .where(Task.owner_id == owner, Task.status.in_(['open', 'snoozed']), Task.due_at == Reminder.reminder_time)).all()
    # Several reminders at once have no unambiguous "that one".
    if len({row.reminder_id for row in recent}) > 1:
        return ParsedCommand('clarify', message='刚提醒了不止一件，做完的是哪一件？')
    return ParsedCommand('complete', task_id=task.id, title=task.title)


def finish_turn(db: Session, owner: str | None, text: str, action: str) -> None:
    key = owner or 'local'
    draft = db.get(PendingTaskDialogue, key)
    if draft and action != 'clarify':
        db.delete(draft)
    elif draft and is_clawbot_owner(owner):
        current = latest_receipt(db, owner)
        draft.last_message_id = current.id if current else None
    focus = db.get(ReminderDialogueFocus, key)
    if focus and action == 'complete':
        db.delete(focus)
    elif focus and action == 'chat' and re.search(r'(?:还没|没|没有|快|差点).*(?:完成|做完|写完|搞定)', text):
        previous = latest_receipt(db, owner, text)
        if focus.expires_at > parser.now_exact_local() and (previous.id if previous else None) == focus.last_message_id:
            current = latest_receipt(db, owner)
            focus.last_message_id = current.id if current else None


def operation_succeeded(db: Session, owner: str | None, action: str) -> None:
    """Consume the draft in the same commit as the task write (crash safety)."""
    key = owner or 'local'
    draft = db.get(PendingTaskDialogue, key)
    if draft is not None:
        db.delete(draft)
    if action == 'complete':
        focus = db.get(ReminderDialogueFocus, key)
        if focus is not None:
            db.delete(focus)
