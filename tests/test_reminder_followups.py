"""Deterministic staged reminders and conversational controls; zero live sends."""
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import clawbot_delivery, reminder_controls, reminder_followups, scheduler
from app.clawbot_identity import clawbot_owner
from app.database import Base, init_db
from app.parser import ParsedCommand
from app.models import (ClawbotDelivery, Reminder, ReminderActionRequest, ReminderFollowupPlan,
                        ReminderFollowupPreference, ReminderStage, Task)
import test_clawbot_delivery as fixtures


class ReminderFollowupTests(TestCase):
    send = fixtures.ClawbotDeliveryTests.send
    claim = fixtures.ClawbotDeliveryTests.claim
    start = fixtures.ClawbotDeliveryTests.start
    result = fixtures.ClawbotDeliveryTests.result

    def setUp(self):
        fixtures.ClawbotDeliveryTests.setUp(self)
        self.now = datetime(2026, 10, 19, 10)
        for timer in (
            patch.dict('os.environ', {'CLAWBOT_FOLLOWUPS_ENABLED': 'true'}),
            patch('app.clawbot_delivery.local_now', side_effect=lambda: self.now),
            patch('app.parser.now_exact_local', side_effect=lambda: self.now),
            patch('app.parser.now_local', side_effect=lambda: self.now.replace(second=0, microsecond=0)),
            patch('app.scheduler.datetime'),
            patch('app.scheduler.SessionLocal', self.sessions),
        ):
            value = timer.start()
            if hasattr(value, 'now'):
                value.now.side_effect = lambda *args: self.now
            self.addCleanup(timer.stop)

    def task(self, minutes=20, priority='high', title='写Java作业', user='Alice'):
        with self.sessions() as db:
            task = Task(title=title, due_at=self.now + timedelta(minutes=minutes),
                        priority=priority, owner_id=clawbot_owner('bot', user),
                        reminder_enabled=True, reminder_channel='clawbot')
            db.add(task)
            db.commit()
            return task.id

    def get_task(self, task_id):
        with self.sessions() as db:
            return db.get(Task, task_id)

    def plan(self, task_id):
        with self.sessions() as db:
            return db.get(ReminderFollowupPlan, task_id)

    def tick(self, minutes=0):
        self.now += timedelta(minutes=minutes)
        scheduler.check_due_tasks()
        scheduler.check_due_tasks()

    def accept(self):
        item = self.claim()
        self.assertIsNotNone(item)
        self.assertEqual(self.start(item).status_code, 200)
        self.assertEqual(self.result(item, message_id='synthetic-' + str(item['id'])).status_code, 200)
        return item

    def count(self):
        with self.sessions() as db:
            return db.scalar(select(func.count()).select_from(ClawbotDelivery))

    def test_high_priority_has_two_advances_due_and_two_bounded_nudges(self):
        tid = self.task()
        self.tick()
        self.assertIn('还有20分钟', self.accept()['text'])
        self.tick(10)
        self.assertIn('还有10分钟', self.accept()['text'])
        self.tick(10)
        due = self.accept()
        self.assertNotIn('计划时间', due['text'])
        self.tick(10)
        self.assertIn('做完了吗', self.accept()['text'])
        self.tick(10)
        self.assertIn('做完了吗', self.accept()['text'])
        self.tick(10)
        self.assertIsNone(self.claim())
        self.assertEqual(self.count(), 5)
        self.assertEqual(self.get_task(tid).status, 'open')

    def test_normal_has_one_nudge_and_low_only_due(self):
        for user, priority in [('Alice', 'normal'), ('Bob', 'low')]:
            self.task(minutes=10, priority=priority, user=user, title=user + '事项')
        self.tick()
        self.assertEqual(self.accept()['sender_id'], 'Alice')
        self.assertIsNone(self.claim())
        self.tick(10)
        self.accept()
        self.accept()
        self.tick(10)
        self.assertEqual(self.accept()['sender_id'], 'Alice')
        self.tick(10)
        self.assertIsNone(self.claim())
        self.assertEqual(self.count(), 4)

    def test_short_task_does_not_receive_backfilled_advance(self):
        self.task(minutes=2)
        self.tick()
        self.assertIsNone(self.claim())
        self.tick(2)
        self.accept()
        self.assertEqual(self.count(), 1)

    def test_completion_after_advance_cancels_every_later_stage(self):
        tid = self.task()
        self.tick()
        self.accept()
        result = self.send('完成了完成了，你别催了').json()
        self.assertEqual(result['task']['id'], tid)
        self.assertEqual(result['task']['status'], 'done')
        self.tick(10)
        self.tick(10)
        self.assertIsNone(self.claim())
        self.assertEqual(self.count(), 1)

    def test_pause_preserves_task_and_short_completion_after_pause_resolves(self):
        tid = self.task(minutes=0)
        self.tick()
        self.accept()
        response = self.send('别催了').json()
        self.assertIn('先不催', response['reply'])
        self.assertEqual(self.get_task(tid).status, 'open')
        self.assertEqual(self.plan(tid).state, 'paused')
        self.assertEqual(self.send('别催了').json(), response)
        self.tick(10)
        self.assertIsNone(self.claim())
        self.assertEqual(self.send('完成了', mid='done').json()['task']['id'], tid)

    def test_defer_changes_next_notification_not_task_deadline(self):
        tid = self.task(minutes=0)
        deadline = self.get_task(tid).due_at
        self.tick()
        self.accept()
        response = self.send('十分钟后再叫我').json()
        self.assertIn('10分钟后', response['reply'])
        self.assertEqual(self.get_task(tid).due_at, deadline)
        self.assertEqual(self.plan(tid).state, 'deferred')
        self.tick(9)
        self.assertIsNone(self.claim())
        self.tick(1)
        self.assertNotIn('做完了吗', self.accept()['text'])
        self.assertEqual(self.plan(tid).state, 'active')
        self.tick(10)
        self.assertIn('做完了吗', self.accept()['text'])

    def test_stop_after_claim_prevents_start(self):
        tid = self.task(minutes=0)
        self.tick()
        item = self.claim()
        self.send('写Java作业别催了')
        self.assertEqual(self.start(item).status_code, 409)
        self.assertEqual(self.get_task(tid).status, 'open')
        self.tick(20)
        self.assertIsNone(self.claim())

    def test_stop_during_send_is_not_undone_by_late_acceptance(self):
        tid = self.task(minutes=0)
        self.tick()
        item = self.claim()
        self.start(item)
        self.send('写Java作业别催了')
        self.result(item, message_id='late')
        self.assertEqual(self.plan(tid).state, 'paused')
        self.tick(10)
        self.assertIsNone(self.claim())

    def test_unknown_advance_halts_future_due_and_followups(self):
        tid = self.task()
        self.tick()
        item = self.claim()
        self.start(item)
        self.result(item, outcome='unknown')
        self.assertEqual(self.plan(tid).state, 'uncertain')
        self.tick(10)
        self.tick(10)
        self.assertIsNone(self.claim())
        self.result(item, message_id='late-accepted')
        self.assertEqual(self.plan(tid).state, 'uncertain')

    def test_sending_timeout_also_halts_future_stages_without_retry(self):
        tid = self.task()
        self.tick()
        item = self.claim()
        self.start(item)
        self.tick(10)
        self.assertIsNone(self.claim())
        self.assertEqual(self.plan(tid).state, 'uncertain')
        with self.sessions() as db:
            self.assertEqual(db.get(ClawbotDelivery, item['id']).status, 'unknown')

    def test_old_due_unknown_ack_cannot_cancel_new_explicit_deferral(self):
        tid = self.task(minutes=0)
        self.tick()
        item = self.claim()
        self.start(item)
        self.send('十分钟后再叫我写Java作业')
        self.result(item, outcome='unknown')
        self.assertEqual(self.plan(tid).state, 'deferred')
        self.tick(10)
        self.accept()

    def test_reschedule_invalidates_claimed_stage_and_resets_plan(self):
        tid = self.task()
        self.tick()
        old = self.claim()
        with self.sessions() as db:
            db.get(Task, tid).due_at += timedelta(hours=2)
            db.commit()
        self.assertEqual(self.start(old).status_code, 409)
        self.tick()
        self.assertEqual(self.plan(tid).task_due_at, self.get_task(tid).due_at)
        self.assertIsNone(self.claim())

    def test_priority_or_enabled_switch_before_start_is_rechecked(self):
        tid = self.task()
        self.tick()
        old = self.claim()
        with self.sessions() as db:
            db.get(Task, tid).priority = 'low'
            db.commit()
        self.assertEqual(self.start(old).status_code, 409)
        self.tick(20)
        due = self.claim()
        with self.sessions() as db:
            db.get(Task, tid).reminder_enabled = False
            db.commit()
        self.assertEqual(self.start(due).status_code, 409)

    def test_pause_and_defer_requests_do_not_cross_owners(self):
        tid = self.task(minutes=0)
        self.tick()
        self.accept()
        self.assertEqual(self.send('写Java作业别催了', user='Bob').json()['action'], 'clarify')
        self.assertEqual(self.send('十分钟后再叫我', user='Bob', mid='2').json()['action'], 'clarify')
        self.assertEqual(self.plan(tid).state, 'active')

    def test_multiple_reminders_accept_short_task_name_and_preserve_other_task(self):
        java = self.task(minutes=0)
        python = self.task(minutes=0, title='写Python作业')
        self.tick()
        self.accept()
        self.accept()
        self.assertEqual(self.send('完成了').json()['action'], 'clarify')
        result = self.send('写Java作业', mid='choose').json()
        self.assertEqual(result['task']['id'], java)
        self.assertEqual(self.get_task(python).status, 'open')

    def test_ordinal_choice_is_not_retargeted_when_candidate_changes(self):
        java = self.task(minutes=0)
        python = self.task(minutes=0, title='写Python作业')
        self.tick()
        self.accept()
        self.accept()
        self.send('别催了')
        with self.sessions() as db:
            db.get(Task, java).due_at += timedelta(hours=1)
            db.commit()
        self.assertEqual(self.send('第一个', mid='choose').json()['action'], 'clarify')
        self.assertNotEqual(self.plan(python).state, 'paused')

    def test_selection_expires_and_unrelated_chat_cannot_complete_task(self):
        tid = self.task(minutes=0)
        self.send('完成了')
        self.send('今天天气真好', mid='chat')
        self.assertNotEqual(self.send('写Java作业', mid='choice').json()['action'], 'complete')
        self.assertEqual(self.get_task(tid).status, 'open')
        self.send('别催了', mid='pause')
        self.now += timedelta(minutes=16)
        self.assertNotIn('先不催', self.send('写Java作业', mid='expired').json()['reply'])

    def test_resume_after_pause_sends_new_attempt_only_at_next_promised_time(self):
        tid = self.task(minutes=0)
        self.tick()
        self.accept()
        self.send('别催了')
        result = self.send('恢复提醒', mid='resume').json()
        self.assertIn('恢复', result['reply'])
        self.tick(9)
        self.assertIsNone(self.claim())
        self.tick(1)
        self.accept()
        self.assertEqual(self.get_task(tid).status, 'open')

    def test_global_opt_out_is_persistent_and_owner_scoped(self):
        self.send('关闭自动跟进')
        self.task(minutes=10, user='Alice')
        self.task(minutes=10, user='Bob')
        self.tick()
        self.assertEqual(self.accept()['sender_id'], 'Bob')
        self.assertIsNone(self.claim())
        with self.sessions() as db:
            pref = db.get(ReminderFollowupPreference, clawbot_owner('bot', 'Alice'))
            self.assertFalse(pref.advance_enabled)
            self.assertFalse(pref.nudge_enabled)
        self.tick(10)
        self.accept()
        self.accept()

    def test_night_skips_automatic_stages_but_honors_explicit_due_and_defer(self):
        self.now = datetime(2026, 10, 19, 23)
        self.task(minutes=10)
        self.tick()
        self.assertIsNone(self.claim())
        self.tick(10)
        self.accept()
        self.send('十分钟后再叫我')
        self.tick(10)
        self.accept()
        self.tick(10)
        self.assertIsNone(self.claim())

    def test_failed_due_or_overdue_bootstrap_does_not_create_followup(self):
        self.task(minutes=0)
        self.tick()
        item = self.claim()
        self.start(item)
        self.result(item, outcome='failed', error_code='synthetic-rejection')
        self.tick(10)
        self.assertIsNone(self.claim())
        self.task(minutes=-30, title='旧事项')
        self.tick()
        self.assertIsNone(self.claim())

    def test_quoted_conditional_or_question_control_never_mutates_plan(self):
        tid = self.task(minutes=0)
        self.tick()
        self.accept()
        for index, text in enumerate(('“别催了”', '如果Java作业别催了', '他说Java作业别催了', '别催了吗？')):
            self.assertEqual(self.send(text, mid=f'quoted-{index}').json()['action'], 'clarify')
        self.assertEqual(self.plan(tid).state, 'active')
        self.assertEqual(self.get_task(tid).status, 'open')

    def test_short_defer_after_creation_reuses_explicit_task_context(self):
        other = self.task(title='写Python作业')
        created = self.send('明天十点提醒我写Java作业').json()
        result = self.send('十分钟后再叫我吧', mid='defer').json()
        self.assertIn('10分钟后', result['reply'])
        self.assertEqual(self.plan(created['task']['id']).state, 'deferred')
        self.assertIsNone(self.plan(other))

    def test_pause_before_due_then_resume_does_not_reuse_cancelled_delivery(self):
        tid = self.task(minutes=0)
        self.tick()
        old = self.claim()
        self.send('写Java作业别催了')
        self.send('恢复提醒', mid='resume')
        self.tick(10)
        new = self.accept()
        self.assertNotEqual(old['id'], new['id'])
        self.assertEqual(self.start(old).status_code, 409)

    def test_global_opt_out_after_claim_prevents_send_but_keeps_due_reminder(self):
        self.task()
        self.tick()
        old = self.claim()
        self.send('关闭提前提醒')
        self.assertEqual(self.start(old).status_code, 409)
        self.tick(20)
        self.accept()

    def test_disabled_task_cannot_be_promised_a_new_reminder(self):
        tid = self.task(minutes=0)
        self.tick()
        self.accept()
        with self.sessions() as db:
            db.get(Task, tid).reminder_enabled = False
            db.commit()
        result = self.send('十分钟后再叫我').json()
        self.assertEqual(result['action'], 'clarify')
        self.assertIn('关闭提醒', result['reply'])

    def test_starting_a_task_in_selection_is_not_reported_as_completed(self):
        tid = self.task(minutes=0)
        self.tick()
        self.accept()
        self.send('今天有点累')
        self.send('完成了', mid='which')
        self.assertNotEqual(self.send('我去写Java作业', mid='start').json()['action'], 'complete')
        self.assertEqual(self.get_task(tid).status, 'open')

    def test_wrong_name_clarification_accepts_next_short_answer(self):
        tid = self.task(minutes=0)
        result = self.send('十分钟后再叫我错误任务').json()
        self.assertEqual(result['action'], 'clarify')
        result = self.send('Java作业', mid='choose').json()
        self.assertIn('10分钟后', result['reply'])
        self.assertEqual(self.plan(tid).state, 'deferred')

    def test_plain_stop_and_not_done_stop_are_not_completion(self):
        tid = self.task(minutes=0)
        self.tick()
        self.accept()
        result = self.send('还没完成，先别催我了').json()
        self.assertIn('先不催', result['reply'])
        self.assertEqual(self.get_task(tid).status, 'open')

    def test_old_database_upgrade_and_reconnect_keep_paused_state(self):
        with TemporaryDirectory() as folder:
            url = 'sqlite:///' + (Path(folder) / 'followups.db').as_posix()
            engine = create_engine(url)
            owner = clawbot_owner('bot', 'Alice')
            try:
                Base.metadata.create_all(engine, tables=[Task.__table__, Reminder.__table__, ClawbotDelivery.__table__])
                with Session(engine) as db:
                    task = Task(title='写Java作业', due_at=self.now + timedelta(minutes=20), priority='high', owner_id=owner)
                    db.add(task)
                    db.commit()
                    tid = task.id
                with patch('app.database.engine', engine):
                    init_db()
                with Session(engine) as db:
                    reminder_followups.queue_stages(db, self.now)
                    reminder_controls.command(db, owner, '写Java作业别催了', '写Java作业别催了')
                    db.commit()
            finally:
                engine.dispose()
            fresh = create_engine(url)
            try:
                with patch('app.database.engine', fresh):
                    init_db()
                with Session(fresh) as db:
                    reminder_followups.queue_stages(db, self.now + timedelta(minutes=10))
                    self.assertEqual(db.get(ReminderFollowupPlan, tid).state, 'paused')
                    self.assertEqual(db.get(Task, tid).status, 'open')
                    self.assertEqual(db.scalar(select(func.count()).select_from(ClawbotDelivery)), 1)
                    self.assertEqual(db.scalar(select(ClawbotDelivery)).status, 'cancelled')
            finally:
                fresh.dispose()

    def test_ready_and_progress_are_not_completion_even_if_model_guesses_wrong(self):
        tid = self.task()
        self.tick()
        self.accept()
        with patch('app.chat_service.parse_with_model', return_value=ParsedCommand('complete', title='写Java作业')):
            for index, text in enumerate(('准备好了', '正在做', '还没完成', '快完成了')):
                result = self.send(text, mid=f'progress-{index}').json()
                self.assertEqual(result['action'], 'chat')
                self.assertEqual(self.get_task(tid).status, 'open')
        result = self.send('做完了', mid='done').json()
        self.assertEqual(result['task']['id'], tid)
        self.assertEqual(result['task']['status'], 'done')

    def test_advance_uses_personal_tone_but_serious_tasks_drop_teasing(self):
        self.send('以后叫我大懒虫')
        self.send('说话俏皮一点', mid='tone')
        self.task()
        self.task(title='去医院复诊')
        self.tick()
        playful = self.claim()['text']
        serious = self.claim()['text']
        self.assertIn('大懒虫', playful)
        self.assertIn('热个身', playful)
        self.assertIn('去医院复诊', serious)
        self.assertNotIn('大懒虫', serious)
        self.assertNotIn('热个身', serious)

    def test_new_reminder_does_not_leave_short_completion_pointing_at_old_paused_task(self):
        java = self.task(minutes=0)
        self.tick()
        self.accept()
        self.send('别催了')
        python = self.task(minutes=0, title='写Python作业')
        self.tick()
        self.accept()
        self.assertEqual(self.send('完成了', mid='done').json()['action'], 'clarify')
        self.assertEqual(self.get_task(java).status, 'open')
        result = self.send('Python作业', mid='which').json()
        self.assertEqual(result['task']['id'], python)
        self.assertEqual(self.plan(java).state, 'paused')
