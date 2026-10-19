"""Multi-turn requests survive new DB sessions; never call a model or WeChat."""
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.clawbot_identity import clawbot_owner
from app.chat_service import handle_chat_message
from app.database import Base, init_db
from app.models import ClawbotMessage, PendingTaskDialogue, Task
import test_clawbot_bridge as fixtures


class DialogueSlotTests(TestCase):
    send = fixtures.ClawbotBridgeTests.send

    def setUp(self):
        fixtures.ClawbotBridgeTests.setUp(self)
        timer = patch('app.parser.now_local', return_value=datetime(2026, 10, 19, 9))
        timer.start()
        self.addCleanup(timer.stop)

    def test_date_then_clock_keeps_original_task_and_deduplicates(self):
        self.assertEqual(self.send('提醒我写Java作业').json()['action'], 'clarify')
        second = self.send('明天吧', mid='2').json()
        self.assertEqual(second['action'], 'clarify')
        self.assertIn('Java', second['reply'])
        result = self.send('下午三点', mid='3').json()
        self.assertEqual(result['action'], 'create')
        self.assertEqual(result['task']['title'], '写Java作业')
        self.assertEqual(result['task']['due_at'], '2026-10-20T15:00:00')
        self.assertEqual(self.send('下午三点', mid='3').json(), result)
        self.assertNotEqual(self.send('下午三点', mid='4').json()['action'], 'create')

    def test_meridiem_then_clock_and_explicit_date_replacement(self):
        self.send('明天提醒我喝水')
        self.assertEqual(self.send('后天', mid='2').json()['action'], 'clarify')
        self.assertEqual(self.send('下午', mid='3').json()['action'], 'clarify')
        result = self.send('三点半吧', mid='4').json()
        self.assertEqual(result['task']['due_at'], '2026-10-21T15:30:00')

    def test_past_time_can_be_corrected_with_date_only(self):
        self.send('今天八点提醒我喝水')
        result = self.send('那就明天', mid='2').json()
        self.assertEqual(result['action'], 'create')
        self.assertEqual(result['task']['due_at'], '2026-10-20T08:00:00')

    def test_initial_partial_period_and_snooze_followup(self):
        self.send('明天下午提醒我喝水')
        result = self.send('三点', mid='2').json()
        self.assertEqual(result['task']['title'], '喝水')
        self.assertEqual(result['task']['due_at'], '2026-10-20T15:00:00')
        self.assertEqual(self.send('把喝水延后', mid='3').json()['action'], 'clarify')
        self.assertEqual(self.send('后天', mid='4').json()['action'], 'clarify')
        changed = self.send('晚上八点', mid='5').json()
        self.assertEqual(changed['action'], 'snooze')
        self.assertEqual(changed['task']['id'], result['task']['id'])
        self.assertEqual(changed['task']['due_at'], '2026-10-21T20:00:00')

    def test_noon_in_title_is_not_a_time_default(self):
        result = self.send('提醒我看中午新闻').json()
        self.assertEqual(result['action'], 'clarify')
        with self.sessions() as db:
            self.assertEqual(db.scalars(select(Task)).all(), [])
        result = self.send('明天下午三点', mid='time').json()
        self.assertEqual(result['task']['title'], '看中午新闻')
        self.assertEqual(result['task']['due_at'], '2026-10-20T15:00:00')

    def test_conflicting_and_invalid_times_preserve_draft_without_writing(self):
        self.send('明天提醒我写Java作业')
        for index, text in enumerate(('25点', '今天明天三点', '三点或四点')):
            self.assertNotEqual(self.send(text, mid=f'invalid-{index}').json()['action'], 'create')
        # A fresh request also replaces an invalidated draft cleanly.
        self.send('明天提醒我写Java作业', mid='fresh')
        result = self.send('下午三点', mid='valid').json()
        self.assertEqual(result['task']['title'], '写Java作业')

    def test_cancel_expiry_other_owner_and_material_do_not_create(self):
        self.send('提醒我写Java作业')
        self.assertNotEqual(self.send('明天下午三点', user='Bob').json()['action'], 'create')
        self.assertIn('先不', self.send('算了不用了', mid='2').json()['reply'])
        self.assertNotEqual(self.send('明天下午三点', mid='3').json()['action'], 'create')
        self.send('提醒我喝水', mid='4')
        with self.sessions() as db:
            draft = db.get(PendingTaskDialogue, clawbot_owner('bot', 'Alice'))
            draft.expires_at = datetime.now() - timedelta(days=1)
            db.commit()
        self.assertNotEqual(self.send('明天下午三点', mid='5').json()['action'], 'create')
        self.send('提醒我喝水', mid='6')
        with self.sessions() as db:
            db.add(ClawbotMessage(external_message_id='material', owner_id=clawbot_owner('bot', 'Alice'),
                                 content='图片', reply_content='资料', status='processed',
                                 response_json='{"action":"material_summary"}'))
            db.commit()
        self.assertNotEqual(self.send('明天下午三点', mid='7').json()['action'], 'create')
        with self.sessions() as db:
            self.assertEqual(db.scalars(select(Task)).all(), [])

    def test_new_request_and_unrelated_date_sentence_do_not_reuse_slots(self):
        self.send('明天提醒我写Java作业')
        result = self.send('后天下午四点提醒我交Python作业', mid='2').json()
        self.assertEqual(result['task']['title'], '交Python作业')
        self.send('提醒我喝水', mid='3')
        self.assertNotEqual(self.send('今天有点累', mid='4').json()['action'], 'create')
        self.assertNotEqual(self.send('明天下午三点', mid='5').json()['action'], 'create')

    def test_model_cannot_rename_validated_pending_task(self):
        from app.parser import ParsedCommand
        self.send('提醒我写Java作业')
        with patch('app.chat_service.parse_with_model', return_value=ParsedCommand('create', title='错误任务')):
            result = self.send('明天下午三点', mid='2').json()
        self.assertEqual(result['task']['title'], '写Java作业')

    def test_ack_and_choice_keep_draft_and_time_question_can_complete(self):
        self.send('明天提醒我写Java作业')
        self.assertEqual(self.send('好的', mid='ack').json()['action'], 'clarify')
        with patch('app.chat_service.parse_with_model', side_effect=AssertionError('ambiguous reply must not call model')):
            self.assertEqual(self.send('下午三点或四点', mid='choice').json()['action'], 'clarify')
        result = self.send('下午三点可以吗？', mid='yes').json()
        self.assertEqual(result['task']['due_at'], '2026-10-20T15:00:00')

    def test_date_is_frozen_across_midnight(self):
        self.send('明天提醒我喝水')
        with patch('app.parser.now_local', return_value=datetime(2026, 10, 20, 0, 5)):
            result = self.send('下午三点', mid='2').json()
        self.assertEqual(result['task']['due_at'], '2026-10-20T15:00:00')

    def test_negated_or_conditional_request_never_seeds_pending(self):
        for index, text in enumerate(('别提醒我喝水', '如果下雨提醒我带伞')):
            self.send(text, mid=f'guard-{index}')
            result = self.send('明天下午三点', mid=f'time-{index}').json()
            self.assertNotEqual(result['action'], 'create')
        with self.sessions() as db:
            self.assertIsNone(db.get(PendingTaskDialogue, clawbot_owner('bot', 'Alice')))

    def test_old_database_upgrade_and_fresh_engine_keep_draft_and_existing_tasks(self):
        with TemporaryDirectory() as folder:
            url = 'sqlite:///' + (Path(folder) / 'isolated.db').as_posix()
            engine = create_engine(url)
            try:
                Base.metadata.create_all(engine, tables=[Task.__table__])
                with Session(engine) as db:
                    db.add(Task(title='已有事项', due_at=datetime(2030, 1, 1)))
                    db.commit()
                with patch('app.database.engine', engine):
                    init_db()
                with Session(engine) as db:
                    handle_chat_message('明天提醒我写Java作业', db, use_model=False)
            finally:
                engine.dispose()
            fresh = create_engine(url)
            try:
                with patch('app.database.engine', fresh):
                    init_db()
                with Session(fresh) as db:
                    result = handle_chat_message('下午三点', db, use_model=False)
                    self.assertEqual(result.action, 'create')
                    self.assertEqual(result.task.due_at, datetime(2026, 10, 20, 15))
                    self.assertEqual(len(db.scalars(select(Task)).all()), 2)
                    self.assertEqual(db.get(Task, 1).title, '已有事项')
                    self.assertIsNone(db.get(PendingTaskDialogue, 'local'))
            finally:
                fresh.dispose()
