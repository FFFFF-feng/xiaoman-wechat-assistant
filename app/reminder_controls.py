"""Explicit reminder controls and short task selection, isolated per owner."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from sqlalchemy import select, update

from . import parser, reminder_followups as followups
from .clawbot_identity import is_clawbot_owner
from .models import (ClawbotDelivery, ConversationTaskContext, Reminder, ReminderActionRequest as Request,
                     ReminderDialogueFocus, ReminderFollowupPreference as Preference, ReminderStage, Task)
from .parser import ParsedCommand
from .task_dialogue import latest_receipt
from . import assistant_preferences, reminder_banter


def _compact(text):
    return re.sub(r'[\s，,。！!~～]', '', text)


def _drop(db, pending):
    if pending:
        db.delete(pending)
        db.flush()


def _remember(db, owner, action, tasks, minutes=None):
    current = latest_receipt(db, owner)
    focus = db.get(ReminderDialogueFocus, owner)
    pending = db.get(Request, owner)
    if pending is None:
        pending = Request(owner_key=owner)
        db.add(pending)
    pending.action, pending.minutes = action, minutes
    pending.candidates_json = json.dumps([{'id': t.id, 'due': t.due_at.isoformat(),
                                          'focus_delivery': focus.delivery_id if focus else None} for t in tasks])
    pending.expires_at = parser.now_exact_local() + timedelta(minutes=15)
    pending.last_message_id = current.id if current else None


def _load_candidates(db, owner, pending):
    candidates = []
    for entry in json.loads(pending.candidates_json):
        task = db.get(Task, entry['id'])
        if task and task.owner_id == owner and task.status in {'open', 'snoozed'} and task.due_at.isoformat() == entry['due']:
            candidates.append(task)
    return candidates


def _match(tasks, text):
    value = _compact(text)
    exact = [t for t in tasks if _compact(t.title) == value]
    return exact or [t for t in tasks if value and value in _compact(t.title)]


def _recent_tasks(db, owner, now):
    rows = db.execute(select(Task, Reminder).join(Reminder, Reminder.task_id == Task.id)
        .join(ClawbotDelivery, ClawbotDelivery.reminder_id == Reminder.id).where(
            Task.owner_id == owner, Task.status.in_(['open', 'snoozed']),
            ClawbotDelivery.owner_id == owner,
            ClawbotDelivery.status.in_(['accepted', 'sending', 'unknown']),
            ClawbotDelivery.started_at >= now - timedelta(minutes=30))).all()
    return list({t.id: t for t, r in rows if followups.task_due(db, r) == t.due_at}.values())


def _targets(db, owner, receipt_text, pending_focus):
    now = parser.now_exact_local()
    focus = db.get(ReminderDialogueFocus, owner)
    if pending_focus:
        tasks = _load_candidates(db, owner, pending_focus)
        recorded = json.loads(pending_focus.candidates_json)
        newer_delivery = focus and recorded and recorded[0].get('focus_delivery') != focus.delivery_id
        item = db.get(ClawbotDelivery, focus.delivery_id) if newer_delivery else None
        reminder = db.get(Reminder, item.reminder_id) if item else None
        if not newer_delivery or (reminder and len(tasks) == 1 and reminder.task_id == tasks[0].id):
            return tasks, len(tasks) == 1
    previous = latest_receipt(db, owner, receipt_text)
    if focus and focus.expires_at > now and focus.last_message_id == (previous.id if previous else None):
        item = db.get(ClawbotDelivery, focus.delivery_id)
        reminder = db.get(Reminder, item.reminder_id) if item else None
        task = db.get(Task, reminder.task_id) if reminder else None
        if (item and item.status == 'accepted' and item.owner_id == owner and task
                and task.owner_id == owner and task.status in {'open', 'snoozed'} and task.due_at == followups.task_due(db, reminder)):
            tasks = _recent_tasks(db, owner, now)
            return tasks, len(tasks) == 1 and tasks[0].id == task.id
    context = db.get(ConversationTaskContext, owner)
    receipt = json.loads(previous.response_json) if previous and previous.status == 'processed' and previous.response_json else {}
    if context and context.expires_at > datetime.now() and receipt.get('action') in {'create', 'snooze'} and (receipt.get('task') or {}).get('id') == context.task_id:
        task = db.get(Task, context.task_id)
        if task and task.owner_id == owner and task.status in {'open', 'snoozed'}:
            return [task], True
    return db.scalars(select(Task).where(Task.owner_id == owner, Task.status.in_(['open', 'snoozed']))
                      .order_by(Task.due_at, Task.id)).all(), False


def _apply(db, owner, action, task, minutes):
    pending = db.get(Request, owner)
    _drop(db, pending)
    if action == 'complete':
        followups.cancel_waiting(db, task.id, 'task_completed')
        return ParsedCommand('complete', task_id=task.id, title=task.title)
    if action != 'pause' and not task.reminder_enabled:
        return ParsedCommand('clarify', message='这件事已关闭提醒，先在日程中打开提醒，再告诉我下次什么时候叫你吧。')
    now = parser.now_exact_local()
    plan = followups.ensure_plan(db, task, now)
    plan.revision += 1
    followups.cancel_waiting(db, task.id)
    if action == 'pause':
        plan.state, plan.deferred_until = 'paused', None
        message = f'好，「{task.title}」后续先不催了，任务还留着。'
    else:
        when = (now + timedelta(minutes=minutes)) if action == 'defer' else (task.due_at if task.due_at > now else now + followups.INTERVAL)
        plan.state, plan.deferred_until, plan.followup_origin = 'deferred', when, None
        plan.due_suppressed = True
        message = (f'好，{minutes}分钟后再叫你做「{task.title}」，事项原来的时间不变。' if action == 'defer'
                   else f'好，恢复「{task.title}」的提醒，下次{when:%m月%d日 %H:%M}叫你。')
    _remember(db, owner, 'focus', [task])
    return ParsedCommand('chat', message=message)


def _global(db, owner, value):
    value = {'以后都不要再催我了': '关闭催促', '以后都别催了': '关闭催促',
             '以后都不要提前提醒': '关闭提前提醒'}.get(value, value)
    settings = {'关闭提前提醒': ('advance_enabled', False), '开启提前提醒': ('advance_enabled', True),
                '关闭催促': ('nudge_enabled', False), '开启催促': ('nudge_enabled', True),
                '关闭自动跟进': ('both', False), '开启自动跟进': ('both', True)}
    if value == '提醒跟进设置':
        advance, nudge = followups.preferences(db, owner)
        active = '已启用' if followups.enabled() else '暂未启用'
        return ParsedCommand('chat', message=f'额外跟进服务{active}；你的提前提醒：{"开" if advance else "关"}，进度询问：{"开" if nudge else "关"}。自动跟进在22点至次日8点休息，明确约定的到点提醒和稍后提醒照常。')
    if value not in settings:
        return None
    pref = db.get(Preference, owner)
    if pref is None:
        pref = Preference(owner_key=owner, advance_enabled=True, nudge_enabled=True)
        db.add(pref)
    field, on = settings[value]
    if field == 'both':
        pref.advance_enabled = pref.nudge_enabled = on
    else:
        setattr(pref, field, on)
    db.flush()
    if not on:
        kinds = ['advance', 'nudge'] if field == 'both' else ['advance'] if field == 'advance_enabled' else ['nudge']
        ids = select(ReminderStage.reminder_id).join(Task, Task.id == ReminderStage.task_id).where(
            Task.owner_id == owner, ReminderStage.kind.in_(kinds))
        waiting = select(ClawbotDelivery.reminder_id).where(ClawbotDelivery.owner_id == owner,
            ClawbotDelivery.reminder_id.in_(ids), ClawbotDelivery.status.in_(['pending', 'claimed']))
        db.execute(update(Reminder).where(Reminder.id.in_(waiting)).values(status='cancelled', error_code='preference_disabled'))
        db.execute(update(ClawbotDelivery).where(ClawbotDelivery.owner_id == owner, ClawbotDelivery.reminder_id.in_(ids),
            ClawbotDelivery.status.in_(['pending', 'claimed'])).values(status='cancelled', claim_hash=None, error_code='preference_disabled'))
    return ParsedCommand('chat', message=f'好，{value}的设置已保存。到点提醒仍按事项安排。' + ('额外跟进服务目前尚未启用。' if not followups.enabled() else ''))


def banter_context(db, owner, text, receipt_text):
    if not is_clawbot_owner(owner) or not reminder_banter.requested(text):
        return None
    pending = db.get(Request, owner)
    previous = latest_receipt(db, owner, receipt_text)
    current = latest_receipt(db, owner)
    if not (pending and pending.action == 'focus' and pending.expires_at > parser.now_exact_local()
            and previous and pending.last_message_id == previous.id):
        pending = None
    tasks, certain = _targets(db, owner, receipt_text, pending)
    task = tasks[0] if certain and len(tasks) == 1 else None
    if task:
        _remember(db, owner, 'focus', [task])
        focus = db.get(ReminderDialogueFocus, owner)
        if (focus and focus.expires_at > parser.now_exact_local() and previous
                and focus.last_message_id == previous.id):
            item = db.get(ClawbotDelivery, focus.delivery_id)
            reminder = db.get(Reminder, item.reminder_id) if item else None
            if item and item.status == 'accepted' and reminder and reminder.task_id == task.id:
                focus.last_message_id = current.id if current else None
    pref = assistant_preferences.snapshot(db, owner)
    return {'task_title': task.title if task else None,
            'fallback': reminder_banter.fallback(task.title if task else None, pref['reply_tone'],
                receipt_text + ':' + str(current.id if current else ''), previous.reply_content if previous else '')}


def command(db, owner, text, receipt_text):
    if not is_clawbot_owner(owner):
        return None
    value = _compact(text)
    global_reply = _global(db, owner, value)
    if global_reply is not None:
        return global_reply
    if reminder_banter.requested(text):
        return None
    if not followups.enabled():
        return None
    if re.search(r'催|再叫我|再提醒我|恢复提醒', text) and re.search(r'如果|假如|要是|他说|她说|[“”「」"？?]|(?:吗|么)$', text):
        return ParsedCommand('clarify', message='这是在讨论，还是要修改提醒？我先不动，确定后再告诉我。')
    pending = db.get(Request, owner)
    if pending:
        previous = latest_receipt(db, owner, receipt_text)
        valid = pending.expires_at > parser.now_exact_local() and previous and previous.status == 'processed' and previous.id == pending.last_message_id
        if not valid:
            _drop(db, pending)
            pending = None
    if pending and pending.action != 'focus':
        if value in {'算了', '不用了', '取消吧'}:
            _drop(db, pending)
            return ParsedCommand('chat', message='好，刚才那次操作先不做。')
        tasks = _load_candidates(db, owner, pending)
        if len(tasks) != len(json.loads(pending.candidates_json)):
            _drop(db, pending)
            return ParsedCommand('clarify', message='这些事项刚有变化，重新告诉我任务名和想做的操作吧，我先不改。')
        exact = [t for t in tasks if _compact(t.title) == value]
        matches = exact or (_match(tasks, value) if not re.search(r'提醒|分钟|别催|完成|取消|如果|假如|[“”"？?]', value) else [])
        ordinal = re.fullmatch(r'第([一二两三四五六七八九1-9])(?:个|件|条)', value)
        if ordinal:
            index = parser._number_value(ordinal[1]) - 1
            matches = tasks[index:index + 1]
        if value in {'就是这个', '就这个', '对', '是的'} and len(tasks) == 1:
            matches = tasks
        if len(matches) == 1:
            return _apply(db, owner, pending.action, matches[0], pending.minutes)
        if len(matches) > 1:
            _remember(db, owner, pending.action, matches, pending.minutes)
            return ParsedCommand('clarify', message='还有几条同名事项，回复第一个或第二个，或补完整的任务名吧。')
        _drop(db, pending)
        pending = None

    action = title = minutes = None
    if re.fullmatch(r'(?:别催了)?(?:我|已经|我已经|都)?(?:完成了|搞定了|做完了|写完了|弄好了|完事了)+(?:你?别催了|不用催了)?', value):
        action = 'complete'
    stopping = r'(?:别(?:再)?催(?:我)?(?:了)?|不要再催(?:我)?(?:了)?|不用再提醒我了|先不催了|不用催了)(?:吧)?'
    pause = re.fullmatch(r'(?:(?:这件事|这个|这条提醒|这条任务|还没完成|没完成|还没做完))?(?:你)?(?:先)?' + stopping, value)
    named_pause = re.fullmatch(r'(?:把)?(.+?)(?:先)?' + stopping, value)
    if action is None and (pause or named_pause):
        action = 'pause'
        title = named_pause[1] if named_pause and not pause else None
    defer = re.fullmatch(r'(?:过)?(' + parser.TIME_NUMBER + r')分钟后(?:再)?(?:叫我|提醒我)(?:一次|一下)?(?:吧|呀)?(.*?)(?:吧|呀)?', value)
    if action is None and defer:
        minutes, title = parser._number_value(defer[1]), defer[2] or None
        if not 1 <= minutes <= 1440:
            return ParsedCommand('clarify', message='想隔多久再叫你？可以设在1到1440分钟之间。')
        action = 'defer'
    resume = re.fullmatch(r'(?:(.+?))?(?:恢复提醒|恢复催促|继续提醒我)', value)
    if action is None and resume:
        action, title = 'resume', resume[1]
    progress = re.fullmatch(r'(?:我)?(?:准备好了|马上做|正在做|还在做|在写了|开始了|还没完成|没完成|还没做完|快完成了|快做完了|收到|知道了|知道啦)', value)
    if action is None and progress:
        tasks, certain = _targets(db, owner, receipt_text, pending if pending and pending.action == 'focus' else None)
        if certain and len(tasks) == 1:
            _remember(db, owner, 'focus', tasks)
        return ParsedCommand('chat', message='好，这件事先留着，做完再告诉我。' if certain else '好，知道啦。')
    if action is None:
        if pending:
            _drop(db, pending)
        return None
    tasks, certain = _targets(db, owner, receipt_text, pending if pending and pending.action == 'focus' else None)
    if title:
        own_tasks = db.scalars(select(Task).where(Task.owner_id == owner, Task.status.in_(['open', 'snoozed']))).all()
        tasks = _match(own_tasks, title)
        certain = len(tasks) == 1
        if not tasks:
            tasks = own_tasks
    if len(tasks) == 1 and certain:
        return _apply(db, owner, action, tasks[0], minutes)
    _drop(db, pending)
    if not tasks:
        return ParsedCommand('clarify', message='还没找到可操作的事项，先创建任务或打开日程确认一下吧。')
    _remember(db, owner, action, tasks, minutes)
    choices = '、'.join(f'{i + 1}.「{t.title}」({t.due_at:%m月%d日 %H:%M})' for i, t in enumerate(tasks[:5]))
    return ParsedCommand('clarify', message='你说的是哪件事？回复任务名就好：' + choices)
