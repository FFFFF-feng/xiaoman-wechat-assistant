"""Local reminder policy; stages reuse the leased outbox, never resend unknowns."""
from __future__ import annotations

import os
import math
import re
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .clawbot_identity import is_clawbot_owner
from .assistant_preferences import snapshot
from .models import (ClawbotDelivery, Reminder, ReminderFollowupPlan as Plan,
                     ReminderFollowupPreference as Preference, ReminderStage as Stage, Task)

LATENESS = timedelta(minutes=5)
INTERVAL = timedelta(minutes=10)


def enabled() -> bool:
    return (os.getenv('CLAWBOT_ENABLED', 'false').lower() == 'true'
            and os.getenv('CLAWBOT_REMINDERS_ENABLED', 'false').lower() == 'true'
            and os.getenv('CLAWBOT_FOLLOWUPS_ENABLED', 'false').lower() == 'true')


def quiet(now: datetime) -> bool:
    # Only automatic advance/nudge messages rest at night; explicit due/defer
    # times remain honored. Missed stages are skipped instead of morning bursts.
    return now.hour >= 22 or now.hour < 8


def preferences(db: Session, owner: str) -> tuple[bool, bool]:
    pref = db.get(Preference, owner)
    return (pref.advance_enabled, pref.nudge_enabled) if pref else (True, True)


def ensure_plan(db: Session, task: Task, now: datetime) -> Plan:
    plan = db.get(Plan, task.id)
    if plan is None:
        plan = Plan(task_id=task.id, task_due_at=task.due_at, revision=0, state='active',
                    due_suppressed=False, created_at=now)
        db.add(plan)
    elif plan.task_due_at != task.due_at:
        plan.task_due_at = task.due_at
        plan.revision += 1
        plan.state, plan.due_suppressed = 'active', False
        plan.deferred_until = plan.followup_origin = None
        plan.created_at = now
    db.flush()
    return plan


def suppresses_due(db: Session, task: Task) -> bool:
    plan = db.get(Plan, task.id)
    return bool(plan and plan.task_due_at == task.due_at
                and (plan.state in {'paused', 'deferred', 'uncertain'} or plan.due_suppressed))


def task_due(db: Session, reminder: Reminder) -> datetime:
    stage = db.get(Stage, reminder.id)
    return stage.task_due_at if stage else reminder.reminder_time


def valid(db: Session, reminder: Reminder, task: Task, now: datetime) -> str | None:
    stage = db.get(Stage, reminder.id)
    plan = db.get(Plan, task.id)
    if stage is None:
        if suppresses_due(db, task):
            return 'paused'
        return None
    if not enabled():
        return 'disabled'
    if not plan or stage.task_due_at != task.due_at or plan.task_due_at != task.due_at or stage.revision != plan.revision:
        return 'superseded'
    if plan.state in {'paused', 'uncertain'}:
        return plan.state
    if stage.kind == 'deferred':
        if plan.state != 'deferred' or plan.deferred_until != reminder.reminder_time:
            return 'superseded'
    elif plan.state != 'active':
        return 'superseded'
    advance, nudge = preferences(db, task.owner_id)
    if stage.kind == 'advance' and (not advance or quiet(now) or now >= task.due_at):
        return 'disabled'
    if stage.kind == 'nudge' and (not nudge or quiet(now)):
        return 'disabled'
    if stage.kind == 'advance':
        offsets = (20, 10) if task.priority == 'high' else (10,) if task.priority == 'normal' else ()
        if int(stage.event_key.split(':')[1]) not in offsets:
            return 'superseded'
    if stage.kind == 'nudge' and int(stage.event_key.split(':')[-1]) > (2 if task.priority == 'high' else 1 if task.priority == 'normal' else 0):
        return 'superseded'
    if reminder.reminder_time > now:
        return 'not_due'
    if now - reminder.reminder_time > LATENESS:
        return 'missed'
    return None


def cancel_waiting(db: Session, task_id: int, reason: str = 'paused') -> None:
    ids = select(Reminder.id).where(Reminder.task_id == task_id)
    waiting = select(ClawbotDelivery.reminder_id).where(
        ClawbotDelivery.reminder_id.in_(ids), ClawbotDelivery.status.in_(['pending', 'claimed']))
    db.execute(update(Reminder).where(Reminder.id.in_(waiting)).values(status='cancelled', error_code=reason))
    db.execute(update(ClawbotDelivery).where(ClawbotDelivery.reminder_id.in_(ids),
        ClawbotDelivery.status.in_(['pending', 'claimed'])).values(status='cancelled', claim_hash=None, error_code=reason))


def _queue(db: Session, task: Task, plan: Plan, when: datetime, kind: str, key: str, now: datetime) -> None:
    if when > now or now - when > LATENESS:
        return
    # First activation does not backfill old stages or old tasks.
    if when < plan.created_at - timedelta(seconds=10):
        return
    if db.scalar(select(Stage).where(Stage.task_id == task.id, Stage.task_due_at == task.due_at,
                                     Stage.event_key == key)):
        return
    # A failed or unknown attempt isn't an excuse to recreate that same stage.
    reminder = Reminder(task_id=task.id, reminder_time=when, channel='clawbot', status='queued')
    db.add(reminder)
    db.flush()
    db.add(Stage(reminder_id=reminder.id, task_id=task.id, task_due_at=task.due_at,
                 revision=plan.revision, kind=kind, event_key=key))
    db.flush()
    from .clawbot_delivery import enqueue
    enqueue(db, reminder, task.owner_id, task.title)


def queue_stages(db: Session, now: datetime) -> None:
    if not enabled():
        return
    tasks = db.scalars(select(Task).where(Task.status.in_(['open', 'snoozed']),
        Task.reminder_enabled.is_(True), Task.owner_id.like('clawbot:%'))).all()
    for task in tasks:
        if not is_clawbot_owner(task.owner_id):
            continue
        plan = ensure_plan(db, task, now)
        if plan.state == 'deferred':
            _queue(db, task, plan, plan.deferred_until, 'deferred', f'deferred:{plan.revision}', now)
            continue
        if plan.state != 'active':
            continue
        advance, nudge = preferences(db, task.owner_id)
        if advance and not quiet(now) and task.due_at > now:
            offsets = (20, 10) if task.priority == 'high' else (10,) if task.priority == 'normal' else ()
            for minutes in offsets:
                when = task.due_at - timedelta(minutes=minutes)
                # Resuming cannot repeat an advance already accepted for this deadline.
                sent = db.scalar(select(Stage).join(ClawbotDelivery, ClawbotDelivery.reminder_id == Stage.reminder_id)
                    .where(Stage.task_id == task.id, Stage.task_due_at == task.due_at,
                           Stage.kind == 'advance', Stage.event_key.like(f'advance:{minutes}:%'),
                           ClawbotDelivery.status.in_(['accepted', 'sending', 'unknown'])))
                if not sent:
                    _queue(db, task, plan, when, 'advance', f'advance:{minutes}:{plan.revision}', now)
        if nudge and not quiet(now) and plan.followup_origin:
            count = 2 if task.priority == 'high' else 1 if task.priority == 'normal' else 0
            for number in range(1, count + 1):
                _queue(db, task, plan, max(plan.followup_origin, task.due_at) + INTERVAL * number,
                       'nudge', f'nudge:{plan.revision}:{number}', now)
    db.commit()


def result(db: Session, item: ClawbotDelivery, outcome: str) -> None:
    reminder = db.get(Reminder, item.reminder_id)
    task = db.get(Task, reminder.task_id) if reminder else None
    if not task:
        return
    plan = db.get(Plan, task.id)
    stage = db.get(Stage, reminder.id)
    if not plan or plan.task_due_at != task.due_at or task_due(db, reminder) != task.due_at:
        return
    if stage and stage.revision != plan.revision:
        return
    if plan.state in {'paused', 'uncertain'}:
        return
    if stage is None and (plan.state != 'active' or plan.due_suppressed):
        return
    if outcome == 'unknown':
        plan.state = 'uncertain'
        cancel_waiting(db, task.id, 'send_outcome_unknown')
    elif outcome == 'accepted' and (stage is None or stage.kind == 'deferred'):
        # Start follow-ups from the actual attempt, not a delayed ACK timestamp.
        plan.state = 'active'
        plan.followup_origin = item.started_at
        plan.deferred_until = None


def stage_text(db: Session, reminder: Reminder, title: str, normal_text: str, now: datetime) -> str:
    stage = db.get(Stage, reminder.id)
    if not stage or stage.kind == 'deferred':
        return normal_text
    task = db.get(Task, reminder.task_id)
    pref = snapshot(db, task.owner_id)
    serious = bool(re.search(r'就医|医院|看病|吃药|服药|航班|火车|赶车|会议|开会|面试|考试|转账|付款|还款|急救', title))
    prefix = f'{pref["user_address"]}，' if pref['user_address'] and not serious else ''
    playful = pref['reply_tone'] == 'playful' and not serious
    if stage.kind == 'advance':
        minutes = max(1, math.ceil((stage.task_due_at - now).total_seconds() / 60))
        return prefix + (f'「{title}」还有{minutes}分钟，先热个身？' if playful
                         else f'「{title}」还有{minutes}分钟，准备好了吗？')
    number = int(stage.event_key.split(':')[-1])
    if playful:
        return prefix + (f'「{title}」搞定没？做完告诉我，我来打勾。' if number == 1
                         else f'来瞅一眼，「{title}」收工了吗？')
    return prefix + (f'「{title}」做完了吗？做好了跟我说一声就行。' if number == 1
                     else f'再问问，「{title}」做完了吗？')
