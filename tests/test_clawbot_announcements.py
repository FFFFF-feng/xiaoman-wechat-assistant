"""Isolated release fan-out, leases and new-member onboarding boundaries."""
from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import patch
from sqlalchemy import select, func
from app import clawbot_announcements as notices, release_catalog
from app.models import ClawbotAnnouncement as Delivery, ClawbotRelease as Published, ClawbotWelcome, Task, Reminder, ScheduleAccess
from app.clawbot_identity import clawbot_owner
from app.local_gateway import allowed_path
import test_clawbot_bridge as fixtures


class AnnouncementTests(TestCase):
    send = fixtures.ClawbotBridgeTests.send

    def setUp(self):
        fixtures.ClawbotBridgeTests.setUp(self)
        self.now = datetime(2026, 10, 17, 7, 0)
        self.alice = clawbot_owner('bot', 'Alice'); self.bob = clawbot_owner('bot', 'Bob')
        self.releases = release_catalog.catalog()
        for p in [patch.dict('os.environ', {'CLAWBOT_ANNOUNCEMENTS_ENABLED':'true', 'PUBLIC_BASE_URL':'https://example.com'}),
                  patch('app.clawbot_announcements.utc_now', side_effect=lambda:self.now),
                  patch('app.clawbot_announcements._worker_seen', 0.0)]:
            p.start(); self.addCleanup(p.stop)
        self.headers = {'Authorization':'Bearer '+fixtures.TOKEN}

    def publish(self, bindings=fixtures.BINDINGS):
        with self.sessions() as db: notices.queue_release(db, bindings)

    def count(self, model=Delivery):
        with self.sessions() as db: return db.scalar(select(func.count()).select_from(model))

    def post(self, path, data=None):
        return self.client.post('/api/internal/clawbot/announcements'+path, json=data or {}, headers=self.headers)

    def claim(self):
        r=self.post('/claim'); self.assertEqual(r.status_code,200,r.text); return r.json()['delivery']

    def start(self, item):
        return self.post(f"/{item['id']}/start", {'claim_token':item['claim_token']})

    def result(self, item, outcome='accepted', **fields):
        return self.post(f"/{item['id']}/result", {'claim_token':item['claim_token'], 'outcome':outcome, **fields})

    def test_latest_release_fans_out_once_without_welcome_or_task_side_effects(self):
        self.publish(); self.publish()
        self.assertEqual(self.count(),2); self.assertEqual(self.count(Published),1)
        for model in (ClawbotWelcome,Task,Reminder): self.assertEqual(self.count(model),0)
        first=self.claim(); second=self.claim()
        self.assertEqual({first['sender_id'],second['sender_id']},{'Alice','Bob'})
        self.assertIn('小满更新啦',first['text']); self.assertIn('/guide?view=updates#release-'+self.releases[0].version.replace('.','-'),first['text'])
        self.assertNotIn('access=',first['text']); self.assertNotIn('你好，我是小满',first['text'])
        self.assertIsNone(self.claim())

    def test_newly_invited_waiting_user_only_gets_onboarding_not_duplicate_update(self):
        with self.sessions() as db:
            db.add(ClawbotWelcome(owner_id=self.alice,status='waiting')); db.commit()
        self.publish()
        self.assertEqual(self.count(),1); self.assertEqual(self.claim()['sender_id'],'Bob')
        self.send('你好，小满')
        self.publish()
        self.assertEqual(self.count(),1)
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(ClawbotWelcome)).status,'pending')

    def test_no_historical_fanout_when_new_member_joins_after_release(self):
        self.publish()
        self.publish([*fixtures.BINDINGS, {'account_id':'bot','sender_id':'Charlie'}])
        self.assertEqual(self.count(),2)

    def test_next_release_notifies_once_and_does_not_reopen_prior_delivery(self):
        self.publish()
        new=self.releases[0].model_copy(update={'version':'0.2.4'})
        with patch('app.release_catalog.catalog',return_value=[new,*self.releases]):
            self.publish(); self.publish()
        self.assertEqual(self.count(),4); self.assertEqual(self.count(Published),2)

    def test_published_release_content_is_immutable(self):
        self.publish()
        changed=self.releases[0].model_copy(update={'summary':'changed after publishing'})
        with patch('app.release_catalog.catalog',return_value=[changed]):
            with self.assertRaises(ValueError): self.publish()
        self.assertEqual(self.count(),2)

    def test_rollback_does_not_broadcast_an_old_version(self):
        self.publish()
        with patch('app.release_catalog.catalog',return_value=[self.releases[1]]): self.publish()
        self.assertEqual(self.count(),2); self.assertEqual(self.count(Published),1)

    def test_binding_rechecked_before_claim_and_before_start(self):
        self.publish()
        with patch('app.clawbot_router.load_bindings',return_value=[fixtures.BINDINGS[1]]):
            item=self.claim(); self.assertEqual(item['sender_id'],'Bob')
        with patch('app.clawbot_router.load_bindings',return_value=[]):
            self.assertEqual(self.start(item).status_code,409)
            self.assertIsNone(self.claim())
        self.assertIsNone(self.claim())

    def test_disabled_does_not_publish_or_send_and_start_rechecks_switch(self):
        with patch.dict('os.environ',{'CLAWBOT_ANNOUNCEMENTS_ENABLED':'false'}):
            self.publish(); self.assertEqual(self.count(),0); self.assertEqual(self.post('/claim').status_code,503)
        self.publish(); item=self.claim()
        with patch.dict('os.environ',{'CLAWBOT_ANNOUNCEMENTS_ENABLED':'false'}):
            self.assertEqual(self.start(item).status_code,503)

    def test_no_public_url_does_not_claim_and_expired_notice_not_sent(self):
        self.publish()
        with patch.dict('os.environ',{'PUBLIC_BASE_URL':''}): self.assertIsNone(self.claim())
        self.now+=timedelta(days=8)
        self.assertIsNone(self.claim())

    def test_expired_claim_recovers_but_unknown_send_never_retries(self):
        self.publish([fixtures.BINDINGS[0]])
        first=self.claim(); self.now+=timedelta(seconds=46); second=self.claim()
        self.assertNotEqual(first['claim_token'],second['claim_token'])
        self.assertEqual(self.start(first).status_code,409)
        self.assertEqual(self.start(second).status_code,200)
        self.assertEqual(self.start(second).status_code,409)
        self.now+=timedelta(seconds=121); self.assertIsNone(self.claim())
        with self.sessions() as db: self.assertEqual(db.get(Delivery,second['id']).status,'unknown')
        self.publish(); self.assertIsNone(self.claim())
        self.assertEqual(self.result(second,message_id='synthetic-id').status_code,200)
        self.assertEqual(self.result(second,message_id='synthetic-id').status_code,200)

    def test_empty_platform_receipt_is_not_success_and_failure_not_requeued(self):
        self.publish([fixtures.BINDINGS[0]]); item=self.claim(); self.start(item)
        self.assertEqual(self.result(item).status_code,422)
        self.assertEqual(self.result(item,'failed',error_code='no_platform_acceptance').status_code,200)
        self.publish(); self.assertIsNone(self.claim())

    def test_user_can_request_updates_without_automatic_onboarding(self):
        r=self.send('最近更新了什么')
        self.assertIn('小满更新啦',r.json()['reply'])
        self.assertIn('/guide?view=updates#release-',r.json()['reply'])
        self.assertEqual(self.count(ClawbotWelcome),0); self.assertEqual(self.count(ScheduleAccess),0)

    def test_imported_update_words_never_execute_commands_or_replay_as_notice(self):
        self.send('整理聊天记录',mid='start')
        r=self.send('最近更新了什么')
        self.assertEqual(r.json()['action'],'import_collect')
        self.assertEqual(self.send('最近更新了什么').json(),r.json())
        self.assertNotIn('/guide?',r.json()['reply'])

    def test_status_is_aggregated_and_endpoints_private(self):
        self.publish()
        path='/api/internal/clawbot/announcements/status'
        self.assertEqual(self.client.get(path).status_code,401)
        r=self.client.get(path,headers=self.headers)
        self.assertEqual(r.json()['releases'][0]['counts'],{'pending':2})
        self.assertNotIn('Alice',r.text); self.assertNotIn(self.alice,r.text)
        for p in (path,'/api/internal/clawbot/announcements/claim','/api/internal/clawbot/announcements/1/start'):
            for method in ('GET','POST'): self.assertFalse(allowed_path(p,method,schedule_only=True))
