"""A short reply targets a real reminder attempt, with owner and state checks."""
from datetime import timedelta
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import select

from app import clawbot_delivery, scheduler
from app.clawbot_identity import clawbot_owner
from app.models import ReminderDialogueFocus, Task
import test_clawbot_delivery as fixtures


class ReminderDialogueTests(TestCase):
    send = fixtures.ClawbotDeliveryTests.send
    queue = fixtures.ClawbotDeliveryTests.queue
    claim = fixtures.ClawbotDeliveryTests.claim
    start = fixtures.ClawbotDeliveryTests.start
    result = fixtures.ClawbotDeliveryTests.result

    def setUp(self):
        fixtures.ClawbotDeliveryTests.setUp(self)

    def accepted(self):
        self.queue()
        item = self.claim()
        self.assertEqual(self.start(item).status_code, 200)
        self.assertEqual(self.result(item, message_id='synthetic-accepted').status_code, 200)
        return item

    def tasks(self):
        with self.sessions() as db:
            return db.scalars(select(Task).order_by(Task.id)).all()

    def test_completion_after_reminder_is_durable_owner_scoped_and_idempotent(self):
        self.accepted()
        response = self.send('完成了完成了，你别催了', mid='finished').json()
        self.assertEqual(response['action'], 'complete')
        self.assertEqual(response['task']['id'], self.tasks()[0].id)
        self.assertEqual(self.tasks()[0].status, 'done')
        self.assertEqual(self.send('完成了完成了，你别催了', mid='finished').json(), response)

    def test_negative_then_completion_keeps_focus_without_false_completion(self):
        self.accepted()
        for index, text in enumerate(('还没完成', '快完成了', '没完成测试任务')):
            self.assertNotEqual(self.send(text, mid=f'no-{index}').json()['action'], 'complete')
            self.assertEqual(self.tasks()[0].status, 'open')
        self.assertEqual(self.send('写完了，别催了', mid='yes').json()['action'], 'complete')

    def test_no_focus_expired_other_owner_and_topic_change_ask_which_task(self):
        self.queue()
        self.assertEqual(self.send('完成了', mid='nofocus').json()['action'], 'clarify')
        item = self.claim()
        self.start(item)
        self.result(item, message_id='receipt')
        self.assertEqual(self.send('完成了', user='Bob').json()['action'], 'clarify')
        with self.sessions() as db:
            focus = db.get(ReminderDialogueFocus, clawbot_owner('bot', 'Alice'))
            focus.expires_at = clawbot_delivery.local_now() - timedelta(seconds=1)
            db.commit()
        self.assertEqual(self.send('完成了', mid='expired').json()['action'], 'clarify')
        self.assertEqual(self.tasks()[0].status, 'open')

    def test_uncertain_send_or_rescheduled_task_is_not_completed(self):
        self.queue()
        item = self.claim()
        self.start(item)
        self.result(item, outcome='unknown')
        self.assertEqual(self.send('完成了', mid='uncertain').json()['action'], 'clarify')
        self.assertEqual(self.tasks()[0].status, 'open')

    def test_intervening_chat_does_not_retarget_old_reminder(self):
        self.accepted()
        self.send('今天有点累', mid='topic')
        self.assertEqual(self.send('完成了', mid='yes').json()['action'], 'clarify')
        self.assertEqual(self.tasks()[0].status, 'open')

    def test_progress_after_topic_change_cannot_revive_old_focus(self):
        self.accepted()
        self.send('今天有点累', mid='topic')
        self.send('没完成', mid='progress')
        self.assertEqual(self.send('完成了', mid='yes').json()['action'], 'clarify')
        self.assertEqual(self.tasks()[0].status, 'open')

    def test_multiple_active_reminders_require_task_name(self):
        self.accepted()
        self.send('明天下午3点提醒我第二件事', mid='second')
        with self.sessions() as db:
            task = db.scalars(select(Task).order_by(Task.id.desc())).first()
            task.due_at = clawbot_delivery.local_now() - timedelta(seconds=1)
            db.commit()
        with patch('app.scheduler.SessionLocal', self.sessions):
            scheduler.check_due_tasks()
        item = self.claim()
        self.start(item)
        self.result(item, message_id='second-receipt')
        self.assertEqual(self.send('完成了', mid='which').json()['action'], 'clarify')
        self.assertTrue(all(t.status == 'open' for t in self.tasks()))

    def test_negation_quotes_questions_cannot_be_overridden_by_model(self):
        from app.parser import ParsedCommand
        self.accepted()
        with patch('app.chat_service.parse_with_model', return_value=ParsedCommand('complete', title=self.tasks()[0].title)):
            for index, text in enumerate(('没完成测试任务', '快做完测试任务了', '他说完成测试任务了', '完成测试任务了吗？', '如果完成测试任务就休息')):
                self.assertNotEqual(self.send(text, mid=f'safe-{index}').json()['action'], 'complete')
        self.assertEqual(self.tasks()[0].status, 'open')

    def test_edit_after_reminder_does_not_complete_new_deadline(self):
        self.accepted()
        with self.sessions() as db:
            task = db.scalar(select(Task))
            task.due_at += timedelta(hours=2)
            db.commit()
        self.assertEqual(self.send('完成了', mid='yes').json()['action'], 'clarify')
        self.assertEqual(self.tasks()[0].status, 'open')
