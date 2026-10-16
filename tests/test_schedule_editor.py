"""Authenticated schedule editing and no-reminder behavior; synthetic DB only."""
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app import clawbot_delivery, database, scheduler
from app.clawbot_identity import clawbot_owner
from app.models import ClawbotDelivery, Reminder, ScheduleCreation, Task
from app.schedule_access import issue_schedule_link
from app.wechat_notifications import NotificationBlocked
import test_clawbot_bridge as fixtures


class ScheduleEditorTests(TestCase):
    def setUp(self):
        fixtures.ClawbotBridgeTests.setUp(self)
        self.future = (datetime.now(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None, microsecond=0) + timedelta(days=1))
        self.alice, self.bob = self.auth('Alice'), self.auth('Bob')

    def auth(self, user):
        with self.sessions() as db:
            token = issue_schedule_link(db, clawbot_owner('bot', user), 'https://example.com').split('#access=')[1]
        return {'Authorization': 'Bearer ' + token}

    def payload(self, **extra):
        return dict(title='去公园散步', due_at=self.future.isoformat(), request_id=str(uuid4()),
                    category='health', priority='high', notes='带水，走30分钟。', reminder_enabled=True, **extra)

    def create(self, payload=None, auth=None):
        return self.client.post('/api/schedule/tasks', headers=auth or self.alice, json=payload or self.payload())

    def count(self, model=Task):
        with self.sessions() as db:
            return db.scalar(select(func.count()).select_from(model))

    def edit(self, task, **changes):
        payload = {k:task[k] for k in ('title', 'due_at', 'category', 'priority', 'notes', 'reminder_enabled')}
        payload.update(expected_updated_at=task['updated_at'], **changes)
        return self.client.post(f'/api/schedule/tasks/{task["id"]}/edit', headers=self.alice, json=payload)

    def test_create_and_edit_require_authorization_and_reject_owner_injection(self):
        self.assertEqual(self.client.post('/api/schedule/tasks', json=self.payload()).status_code, 401)
        self.assertEqual(self.client.post('/api/schedule/tasks/1/edit', json={}).status_code, 401)
        data = self.payload(); data['owner_id'] = clawbot_owner('bot', 'Bob')
        self.assertEqual(self.create(data).status_code, 422)
        self.assertEqual(self.count(), 0)

    def test_create_preserves_all_fields_timezone_and_trusted_owner(self):
        data = self.payload(); data['due_at'] = self.future.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc).isoformat()
        result = self.create(data)
        self.assertEqual(result.status_code, 200, result.text)
        task = result.json()
        for key in ('title', 'category', 'priority', 'notes', 'reminder_enabled'):
            self.assertEqual(task[key], data[key])
        self.assertEqual(task['due_at'], self.future.isoformat())
        self.assertEqual(task['reminder_channel'], 'clawbot_pending')
        with self.sessions() as db:
            self.assertEqual(db.get(Task, task['id']).owner_id, clawbot_owner('bot', 'Alice'))

    def test_replayed_creation_is_cached_and_changed_content_conflicts(self):
        data = self.payload(); first = self.create(data)
        self.assertEqual(self.create(data).json(), first.json())
        self.assertEqual(self.create({**data, 'title':'改变正文'}).status_code, 409)
        self.assertEqual(self.count(), 1); self.assertEqual(self.count(ScheduleCreation), 1)

    def test_same_request_id_does_not_mix_users(self):
        data = self.payload(); a = self.create(data).json(); b = self.create(data, self.bob).json()
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual(self.count(), 2)

    def test_invalid_fields_cannot_create_a_task(self):
        for change in ({'title':'  '}, {'priority':'urgent'}, {'category':'admin'}, {'notes':'a'*2001},
                       {'reminder_enabled':'false'}, {'request_id':'invalid'}, {'due_at':'not a date'}):
            result = self.create({**self.payload(), **change})
            self.assertEqual(result.status_code, 422, result.text)
        self.assertEqual(self.count(), 0)

    def test_past_reminder_rejected_but_record_only_can_backfill(self):
        data = self.payload(); data['due_at'] = (self.future-timedelta(days=2)).isoformat()
        self.assertEqual(self.create(data).status_code, 422)
        data['reminder_enabled'] = False
        with patch('app.schedule_router.plan_reminder', side_effect=AssertionError('record only must not reserve a send')):
            result = self.create(data)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()['reminder_enabled'])

    def test_edit_saves_fields_and_uses_current_version(self):
        task = self.create().json()
        due = (self.future+timedelta(days=2)).isoformat()
        result = self.edit(task, title='改成读书', due_at=due, category='study', priority='low',
                           notes='读完第一章。', reminder_enabled=False)
        self.assertEqual(result.status_code, 200, result.text)
        new = result.json()
        self.assertEqual(new['id'], task['id']); self.assertEqual(new['title'], '改成读书')
        self.assertEqual(new['due_at'], due); self.assertEqual(new['category'], 'study')
        self.assertEqual(new['notes'], '读完第一章。'); self.assertFalse(new['reminder_enabled'])
        self.assertIsNone(new['reminder_channel']); self.assertEqual(self.count(), 1)
        self.assertEqual(self.edit(task, title='旧版本覆盖').status_code, 409)
        with self.sessions() as db:
            self.assertEqual(db.get(Task, task['id']).title, '改成读书')

    def test_other_user_and_ended_tasks_cannot_be_edited(self):
        task = self.create(auth=self.bob).json()
        self.assertEqual(self.edit(task, title='跨用户').status_code, 404)
        for action in ('cancel', 'complete'):
            task = self.create().json()
            self.assertEqual(self.client.post(f'/api/schedule/tasks/{task["id"]}/{action}', headers=self.alice).status_code, 200)
            self.assertEqual(self.edit(task, title='修改已结束任务').status_code, 409)

    def test_notification_rejection_leaves_no_task_or_receipt(self):
        with patch('app.schedule_router.plan_reminder', side_effect=NotificationBlocked('渠道不可用', 'configuration')):
            self.assertEqual(self.create().status_code, 422)
        self.assertEqual(self.count(), 0); self.assertEqual(self.count(ScheduleCreation), 0)

    def test_record_only_snooze_does_not_require_notification_qualification(self):
        data = self.payload(); data['reminder_enabled'] = False; task = self.create(data).json()
        with patch('app.schedule_router.plan_reminder', side_effect=AssertionError('record only')):
            response = self.client.post(f'/api/schedule/tasks/{task["id"]}/snooze', headers=self.alice,
                                       json={'due_at':(self.future+timedelta(hours=1)).isoformat()})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()['reminder_enabled'])

    def test_record_only_is_excluded_from_due_scheduler(self):
        data = self.payload(); data.update(reminder_enabled=False, due_at=(self.future-timedelta(days=2)).isoformat())
        self.assertEqual(self.create(data).status_code, 200)
        with patch('app.scheduler.SessionLocal', self.sessions), patch.object(scheduler.adapter, 'send') as sender:
            scheduler.check_due_tasks(); sender.assert_not_called()
        self.assertEqual(self.count(Reminder), 0); self.assertEqual(self.count(ClawbotDelivery), 0)

    def test_disabling_reminder_invalidates_already_queued_delivery(self):
        task = self.create().json()
        with self.sessions() as db:
            obj = db.get(Task, task['id']); obj.due_at = clawbot_delivery.local_now()-timedelta(seconds=5)
            db.commit()
        with patch.dict('os.environ', {'CLAWBOT_REMINDERS_ENABLED':'true'}), patch('app.scheduler.SessionLocal', self.sessions):
            scheduler.check_due_tasks()
            self.assertEqual(self.count(ClawbotDelivery), 1)
            with self.sessions() as db:
                db.get(Task, task['id']).reminder_enabled = False; db.commit()
                self.assertIsNone(clawbot_delivery.claim(db, fixtures.BINDINGS))
                self.assertEqual(db.scalar(select(ClawbotDelivery)).status, 'cancelled')


class ScheduleMigrationTests(TestCase):
    def test_existing_tasks_keep_values_and_receive_safe_defaults(self):
        engine = create_engine('sqlite://')
        try:
            with engine.begin() as connection:
                connection.execute(text('CREATE TABLE tasks (id INTEGER PRIMARY KEY, title VARCHAR(300) NOT NULL, '
                    'due_at DATETIME NOT NULL, status VARCHAR(30) NOT NULL, priority VARCHAR(20) NOT NULL, '
                    'created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)'))
                connection.execute(text("INSERT INTO tasks VALUES(1,'旧日程','2027-01-01 10:00:00','open','high',"
                                        "'2026-10-01 10:00:00','2026-10-01 10:00:00')"))
            with patch.object(database, 'engine', engine):
                database.init_db(); database.init_db()
            with Session(engine) as db:
                task = db.get(Task, 1)
                self.assertEqual(task.title, '旧日程'); self.assertEqual(task.priority, 'high')
                self.assertEqual(task.category, 'life'); self.assertEqual(task.notes, '')
                self.assertTrue(task.reminder_enabled)
        finally:
            engine.dispose()
