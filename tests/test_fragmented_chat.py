"""Offline batch intent and concise confirmation regression; no real model/Weixin."""
from datetime import datetime
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import func, select

from app.chat_service import _friendly_due
from app.models import Task
import test_clawbot_bridge as fixtures


class FragmentedChatTests(TestCase):
    send = fixtures.ClawbotBridgeTests.send

    def setUp(self):
        fixtures.ClawbotBridgeTests.setUp(self)
        now = datetime(2026, 10, 13, 9, 0)
        for target in ('app.parser.now_local', 'app.chat_service.now_local'):
            timer = patch(target, return_value=now)
            timer.start(); self.addCleanup(timer.stop)

    def tasks(self):
        with self.sessions() as db:
            return db.scalar(select(func.count()).select_from(Task))

    def test_one_reminder_split_across_messages_creates_once(self):
        text = '提醒我\n明天\n上午十点\n写周报'
        result = self.send(text, mid='batch-test')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['action'], 'create')
        self.assertEqual(result.json()['reply'], '记下啦，明天 10:00提醒你写周报。自动提醒暂不可用。')
        self.assertEqual(self.send(text, mid='batch-test').json(), result.json())
        self.assertEqual(self.tasks(), 1)

    def test_later_negation_or_withdrawal_never_creates_early_request(self):
        for n, tail in enumerate(('算了，不要提醒我', '算了', '不用了', '算了，不用了', '先别', '先别创建了')):
            result = self.send('明天十点提醒我写周报\n' + tail, mid=f'batch-{n}')
            self.assertEqual(result.json()['action'], 'clarify')
        self.assertEqual(self.tasks(), 0)

    def test_later_condition_does_not_create_fixed_time_task(self):
        result = self.send('明天十点提醒我带伞\n如果下雨就提醒我', mid='batch-condition')
        self.assertEqual(result.json()['action'], 'clarify'); self.assertEqual(self.tasks(), 0)

    def test_multiple_tasks_or_conflicting_times_ask_once_before_mutation(self):
        for n, text in enumerate(('明天十点提醒我写周报\n明天十一点提醒我喝水',
                                  '明天十点提醒我写周报\n改成十一点',
                                  '明天十点提醒我写周报\n不对，后天')):
            result = self.send(text, mid=f'batch-conflict-{n}')
            self.assertEqual(result.json()['action'], 'clarify')
        self.assertEqual(self.tasks(), 0)

    def test_task_title_negation_is_not_a_withdrawal(self):
        result = self.send('明天十点提醒我\n不要忘记喝水', mid='batch-title')
        self.assertEqual(result.json()['action'], 'create'); self.assertEqual(self.tasks(), 1)

    def test_enabled_confirmation_and_reschedule_are_short_and_nontechnical(self):
        with patch.dict('os.environ', {'CLAWBOT_REMINDERS_ENABLED': 'true'}):
            result = self.send('明天十点提醒我写周报')
            self.assertEqual(result.json()['reply'], '记下啦，明天 10:00提醒你写周报。')
            result = self.send('把写周报延后到后天十一点', mid='2')
            self.assertEqual(result.json()['action'], 'snooze')
            self.assertEqual(result.json()['reply'], '好，「写周报」改到后天 11:00。')
        self.assertNotIn('连接', result.json()['reply']); self.assertNotIn('保持', result.json()['reply'])

    def test_friendly_date_preserves_cross_year_and_local_clock(self):
        self.assertEqual(_friendly_due(datetime(2026, 10, 13, 17, 5)), '今天 17:05')
        self.assertEqual(_friendly_due(datetime(2026, 10, 20, 17, 5)), '10月20日 17:05')
        self.assertEqual(_friendly_due(datetime(2027, 1, 1, 17, 5)), '2027年01月01日 17:05')
