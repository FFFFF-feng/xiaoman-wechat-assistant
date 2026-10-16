"""Behavioral regressions for defaults and owner-bound follow-up edits; no live sends."""
import json
from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import select

from app.chat_service import _execute_command, _reconcile_commands, handle_chat_message
from app.clawbot_identity import clawbot_owner
from app.models import ClawbotMessage, ConversationTaskContext, Task
from app.parser import ParsedCommand, correction_time_text, parse_command
import test_clawbot_bridge as fixtures


class NaturalReminderTests(TestCase):
    send = fixtures.ClawbotBridgeTests.send

    def setUp(self):
        fixtures.ClawbotBridgeTests.setUp(self)
        self.now = datetime(2026, 10, 16, 9, 0)
        for target in ('app.parser.now_local', 'app.chat_service.now_local'):
            timer = patch(target, return_value=self.now)
            timer.start(); self.addCleanup(timer.stop)

    def all_tasks(self):
        with self.sessions() as db:
            return db.scalars(select(Task).order_by(Task.id)).all()

    def test_noon_default_is_disclosed_and_retry_creates_only_once(self):
        reply = self.send('明天中午提醒我取快递')
        data = reply.json()
        self.assertEqual(data['action'], 'create')
        self.assertEqual(data['task']['due_at'], '2026-10-17T12:00:00')
        self.assertEqual(data['task']['title'], '取快递')
        self.assertIn('先按12:00', data['reply'])
        self.assertEqual(self.send('明天中午提醒我取快递').json(), data)
        self.assertEqual(len(self.all_tasks()), 1)

    def test_noon_in_both_word_orders_and_explicit_clock_wins(self):
        for text in ('中午提醒我取快递', '提醒我今天中午取快递'):
            command = parse_command(text)
            self.assertEqual(command.due_at, self.now.replace(hour=12))
            self.assertTrue(command.assumed_time)
            self.assertEqual(command.title, '取快递')
        command = parse_command('明天中午12:30提醒我取快递')
        self.assertEqual(command.due_at, datetime(2026, 10, 17, 12, 30))
        self.assertFalse(command.assumed_time)

    def test_missing_time_can_be_completed_with_noon(self):
        self.assertEqual(self.send('提醒我取快递').json()['action'], 'clarify')
        result = self.send('明天中午', mid='2').json()
        self.assertEqual(result['action'], 'create')
        self.assertEqual(result['task']['due_at'], '2026-10-17T12:00:00')
        self.assertIn('先按12:00', result['reply'])

    def test_web_chat_uses_same_task_reference(self):
        with self.sessions() as db:
            first = handle_chat_message('明天中午提醒我取快递', db, use_model=False)
            second = handle_chat_message('改成12:30', db, use_model=False)
            self.assertEqual(second.action, 'snooze')
            self.assertEqual(second.task.id, first.task.id)
            self.assertEqual(second.task.due_at, datetime(2026, 10, 17, 12, 30))

    def test_explicit_clock_and_ampm_are_preserved(self):
        self.assertEqual(parse_command('明天12:30提醒我取快递').due_at.hour, 12)
        command = parse_command('明天1:30pm提醒我取快递')
        self.assertEqual(command.due_at, datetime(2026, 10, 17, 13, 30))
        self.assertEqual(parse_command('明天1:30PM提醒我取快递').due_at, command.due_at)

    def test_past_noon_is_not_rolled_to_tomorrow(self):
        with patch('app.parser.now_local', return_value=self.now.replace(hour=13)):
            result = self.send('中午提醒我取快递').json()
        self.assertEqual(result['action'], 'clarify')
        self.assertEqual(self.all_tasks(), [])

    def test_noon_mentions_negations_and_conditions_do_not_get_created(self):
        for n, text in enumerate(('不要中午提醒我取快递', '别提醒我明天中午取快递',
                                  '如果中午下雨就提醒我带伞', '中午之前提醒我取快递',
                                  '提醒我看《中午》这本书', '晚点提醒我取快递')):
            self.assertEqual(self.send(text, mid=str(n)).json()['action'], 'clarify')
        self.assertEqual(self.all_tasks(), [])

    def test_old_model_time_question_cannot_block_noon_convention(self):
        text = '明天中午提醒我取快递'
        command = _reconcile_commands(text, ParsedCommand('clarify', message='几点呢？'), parse_command(text), None)
        self.assertEqual(command.action, 'create')
        self.assertTrue(command.assumed_time)
        command = _reconcile_commands(text, ParsedCommand('chat', message='在聊中午。'), parse_command(text), None)
        self.assertEqual(command.action, 'chat')

    def test_correction_keeps_task_id_and_original_date_without_model(self):
        first = self.send('明天中午提醒我取快递').json()
        with patch('app.chat_service.parse_with_model', side_effect=AssertionError('should not call model')):
            second = self.send('改成12:30', mid='2').json()
        self.assertEqual(second['action'], 'snooze')
        self.assertEqual(second['task']['id'], first['task']['id'])
        self.assertEqual(second['task']['due_at'], '2026-10-17T12:30:00')
        self.assertEqual(self.send('改成12:30', mid='2').json(), second)
        third = self.send('改到后天13:00', mid='3').json()
        self.assertEqual(third['task']['id'], first['task']['id'])
        self.assertEqual(third['task']['due_at'], '2026-10-18T13:00:00')
        self.assertEqual(len(self.all_tasks()), 1)

    def test_same_titles_do_not_retarget_a_different_task(self):
        first = self.send('明天十点提醒我取快递').json()
        second = self.send('后天十一点提醒我取快递', mid='2').json()
        corrected = self.send('改成12:30', mid='3').json()
        self.assertEqual(corrected['task']['id'], second['task']['id'])
        self.assertEqual(self.all_tasks()[0].due_at, datetime(2026, 10, 17, 10))
        self.assertNotEqual(first['task']['id'], corrected['task']['id'])

    def test_no_context_expired_context_and_different_owner_do_not_modify(self):
        self.assertEqual(self.send('改成12:30').json()['action'], 'clarify')
        self.send('明天中午提醒我取快递', mid='2')
        self.assertEqual(self.send('改成12:30', user='Bob').json()['action'], 'clarify')
        with self.sessions() as db:
            context = db.get(ConversationTaskContext, clawbot_owner('bot', 'Alice'))
            context.expires_at = datetime.now() - timedelta(seconds=1)
            db.commit()
        self.assertEqual(self.send('改成12:30', mid='3').json()['action'], 'clarify')
        self.assertEqual(self.all_tasks()[0].due_at, datetime(2026, 10, 17, 12))

    def test_intervening_chat_or_materials_invalidates_reference(self):
        self.send('明天中午提醒我取快递')
        self.send('今天天气不错', mid='2')
        self.assertEqual(self.send('改成12:30', mid='3').json()['action'], 'clarify')
        self.send('明天中午提醒我喝水', mid='4')
        with self.sessions() as db:
            db.add(ClawbotMessage(external_message_id='synthetic-material', owner_id=clawbot_owner('bot', 'Alice'),
                content='图片', reply_content='合成图片回应', status='processed',
                response_json=json.dumps({'action': 'material_summary'})))
            db.commit()
        self.assertEqual(self.send('改成12:30', mid='5').json()['action'], 'clarify')
        self.assertTrue(all(t.due_at.hour == 12 for t in self.all_tasks()))

    def test_failed_or_unknown_intervening_receipt_cannot_reuse_old_reference(self):
        for n, status in enumerate(('failed', 'processing')):
            self.send('明天中午提醒我取快递', mid=f'create-{n}')
            with self.sessions() as db:
                db.add(ClawbotMessage(external_message_id=f'failed-{n}', owner_id=clawbot_owner('bot', 'Alice'),
                                     content='附件', status=status))
                db.commit()
            self.assertEqual(self.send('改成12:30', mid=f'edit-{n}').json()['action'], 'clarify')
        self.assertTrue(all(t.due_at.hour == 12 for t in self.all_tasks()))

    def test_past_correction_never_creates_or_rolls_over(self):
        self.send('今天中午提醒我取快递')
        data = self.send('改成08:00', mid='2').json()
        self.assertEqual(data['action'], 'clarify')
        self.assertEqual(self.all_tasks()[0].due_at, datetime(2026, 10, 16, 12))

    def test_negated_or_conditional_correction_cannot_be_overridden_by_model(self):
        self.send('明天中午提醒我取快递')
        with patch('app.chat_service.parse_with_model', return_value=ParsedCommand('snooze', due_at=self.now.replace(hour=15))):
            for n, text in enumerate(('不要改成12:30', '如果有空就改到12:30')):
                self.assertEqual(self.send(text, mid=f'neg-{n}').json()['action'], 'clarify')
        self.assertEqual(self.all_tasks()[0].due_at, datetime(2026, 10, 17, 12))

    def test_cancelled_task_and_wrong_owner_task_id_cannot_be_modified(self):
        self.send('明天中午提醒我取快递')
        task = self.all_tasks()[0]
        with self.sessions() as db:
            result = _execute_command(ParsedCommand('snooze', task_id=task.id,
                due_at=datetime(2026, 10, 17, 13)), db, owner_id=clawbot_owner('bot', 'Bob'))
            self.assertIsNone(result.task)
            db.get(Task, task.id).status = 'cancelled'; db.commit()
        self.assertEqual(self.send('改成12:30', mid='2').json()['action'], 'clarify')
        self.assertEqual(self.all_tasks()[0].status, 'cancelled')

    def test_correction_grammar_rejects_conflicting_dates_times_and_extra_commands(self):
        for n, text in enumerate(('改成今天明天12:30', '改成12点或13点', '改成12:30取消喝水', '改成12:30\n明天13点提醒我喝水')):
            self.assertIsNone(correction_time_text(text))
            with patch('app.chat_service.parse_with_model', side_effect=AssertionError('should not call model')):
                self.assertEqual(self.send(text, mid=f'invalid-{n}').json()['action'], 'clarify')
        for text in ('明天25:00提醒我喝水', '13月40号12点提醒我喝水'):
            self.assertEqual(parse_command(text).action, 'clarify')
