"""Isolated database/allowlist tests. No real QR login or phone identity is simulated as verified."""
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import select

import test_clawbot_bridge as fixtures
from app import clawbot_enrollment as enrollment, clawbot_delivery, scheduler
from app.clawbot_bindings import load_bindings
from app.clawbot_identity import clawbot_owner
from app.join_page import join_page_response
from app.local_gateway import allowed_path
from app.models import ClawbotInvitation, ClawbotDelivery, ClawbotWelcome, Task, Reminder, ScheduleAccess
from app.schedule_access import issue_schedule_link, utc_now


class EnrollmentTests(TestCase):
    def setUp(self):
        fixtures.ClawbotBridgeTests.setUp(self)
        self.patches[1].stop()  # Exercise real private allowlist persistence, not a mock reader.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "bindings.json"
        self.path.write_text(json.dumps([fixtures.BINDINGS[0]]), encoding="utf-8")
        flags = patch.dict("os.environ", {"CLAWBOT_BINDINGS_FILE": str(self.path), "CLAWBOT_ENROLLMENT_ENABLED": "true",
                                         "CLAWBOT_MAX_USERS": "3", "CLAWBOT_REMINDERS_ENABLED": "true"})
        flags.start(); self.addCleanup(flags.stop)
        self.client.app.include_router(enrollment.public_router)
        self.client.app.include_router(enrollment.admin_router)
        self.admin = {"Authorization": f"Bearer {fixtures.TOKEN}"}
        seen = patch.object(enrollment, "_worker_seen", utc_now())
        seen.start(); self.addCleanup(seen.stop)
        codes = patch.dict(enrollment._codes, {}, clear=True)
        codes.start(); self.addCleanup(codes.stop)

    def post(self, route, data=None, headers=None):
        if route == "/api/join/start" and data is None:
            data = {"consent": True}
        return self.client.post(route, json=data or {}, headers=headers or self.admin)

    def new(self):
        result = self.post("/api/internal/clawbot/enrollment/invites", {"label": "测试邀请"})
        self.assertEqual(result.status_code, 200, result.text)
        values = result.json()
        return values["id"], {"Authorization": f"Bearer {values['invite']}"}, values["invite"]

    def started(self):
        job_id, headers, token = self.new()
        result = self.post("/api/join/start", headers=headers)
        self.assertEqual(result.status_code, 200, result.text)
        job = self.post("/api/internal/clawbot/enrollment/claim").json()["job"]
        self.assertEqual(job["id"], job_id)
        return job, headers, token

    def complete(self, job, account="bot-two", user="Bob"):
        return self.post(f"/api/internal/clawbot/enrollment/{job['id']}/complete",
                         {"lease": job["lease"], "account_id": account, "sender_id": user})

    def send(self, account="bot", user="Alice", mid="same", content="两分钟后提醒我喝水"):
        return self.post("/api/internal/clawbot/chat", {"account_id": account, "sender_id": user,
                                                     "message_id": mid, "content": content})

    def test_invitation_hash_only_and_private_api_not_public(self):
        job, headers, token = self.new()
        with self.sessions() as db:
            self.assertNotEqual(db.get(ClawbotInvitation, job).token_hash, token)
        self.assertEqual(self.client.get("/api/join/status").status_code, 401)
        self.assertEqual(self.client.get("/api/join/status", headers=self.admin).status_code, 401)
        self.assertEqual(self.client.post("/api/internal/clawbot/enrollment/invites", json={"label":"test"}).status_code, 401)
        self.assertFalse(allowed_path("/api/internal/clawbot/enrollment/invites", "POST", schedule_only=True))
        self.assertTrue(allowed_path("/join", "GET", schedule_only=True))
        self.assertTrue(allowed_path("/api/join/start", "POST", schedule_only=True))
        self.assertFalse(allowed_path("/api/join/status", "POST", schedule_only=True))
        self.assertFalse(allowed_path("/api/join/start/", "POST", schedule_only=True))

    def test_append_does_not_replace_and_completion_is_idempotent(self):
        job, headers, _ = self.started()
        self.assertEqual(self.complete(job).status_code, 200)
        self.assertEqual(self.complete(job).status_code, 200)
        self.assertEqual(load_bindings(), [fixtures.BINDINGS[0], {"account_id":"bot-two", "sender_id":"Bob"}])
        view = self.client.get("/api/join/status", headers=headers).json()
        self.assertEqual(view["state"], "connected")
        self.assertNotIn("account_id", view)
        self.assertNotIn("lease", view)
        with self.sessions() as db:
            self.assertEqual(len(db.scalars(select(ClawbotWelcome)).all()), 1)
            self.assertEqual(db.scalar(select(ClawbotWelcome)).status, "waiting")

    def welcome(self):
        with patch.dict('os.environ', {'PUBLIC_BASE_URL': 'https://example.com'}):
            return self.post('/api/internal/clawbot/welcome/claim').json()['delivery']

    def new_welcome(self):
        job, _, _ = self.started()
        self.complete(job)
        self.send(account='bot-two', user='Bob', content='查询待办')
        return self.welcome()

    def test_welcome_waits_for_first_message_then_targets_only_new_user(self):
        job, _, _ = self.started(); self.complete(job)
        self.assertIsNone(self.welcome())
        self.send(content='查询待办')  # Original user: no unsolicited welcome.
        self.assertIsNone(self.welcome())
        self.assertEqual(self.send(account='bot-two', user='Bob', content='查询待办').status_code, 200)
        item = self.welcome()
        self.assertEqual((item['account_id'], item['sender_id']), ('bot-two', 'Bob'))
        self.assertIn('[点击查看小满使用指南](https://example.com/guide)', item['text'])
        self.assertIsNone(self.welcome())
        with self.sessions() as db:
            self.assertEqual(db.scalars(select(Task)).all(), [])
            self.assertEqual(db.scalars(select(Reminder)).all(), [])

    def test_welcome_requires_reachable_origin_authentication_and_private_routes(self):
        job, _, _ = self.started(); self.complete(job)
        self.send(account='bot-two', user='Bob', content='查询待办')
        self.assertIsNone(self.post('/api/internal/clawbot/welcome/claim').json()['delivery'])
        for path in ('/api/internal/clawbot/welcome/claim', '/api/internal/clawbot/welcome/1/start'):
            self.assertFalse(allowed_path(path, 'POST', schedule_only=True))
            self.assertEqual(self.client.post(path, json={}).status_code, 401)
        self.assertIsNotNone(self.welcome())

    def test_welcome_accepted_is_durable_and_does_not_repeat(self):
        item = self.new_welcome(); prefix = f"/api/internal/clawbot/welcome/{item['id']}"
        lease = {'claim_token': item['claim_token']}
        self.assertEqual(self.post(prefix+'/start', lease).status_code, 200)
        self.assertEqual(self.post(prefix+'/start', lease).status_code, 409)
        receipt = {**lease, 'outcome':'accepted', 'message_id':'native-welcome'}
        self.assertEqual(self.post(prefix+'/result', receipt).status_code, 200)
        self.assertEqual(self.post(prefix+'/result', receipt).status_code, 200)
        self.send(account='bot-two', user='Bob', mid='next', content='查询待办')
        self.assertIsNone(self.welcome())
        with self.sessions() as db:
            self.assertEqual(db.get(ClawbotWelcome, item['id']).external_message_id, 'native-welcome')

    def test_welcome_revoke_before_send_and_wrong_lease_blocked(self):
        item = self.new_welcome(); prefix = f"/api/internal/clawbot/welcome/{item['id']}"
        self.assertEqual(self.post(prefix+'/start', {'claim_token':'b'*64}).status_code, 409)
        owner = clawbot_owner('bot-two', 'Bob')
        self.post('/api/internal/clawbot/enrollment/members/'+owner+'/revoke')
        self.assertEqual(self.post(prefix+'/start', {'claim_token': item['claim_token']}).status_code, 409)
        self.assertIsNone(self.welcome())

    def test_welcome_expired_claim_recovers_but_uncertain_send_never_resends(self):
        item = self.new_welcome()
        with self.sessions() as db:
            db.get(ClawbotWelcome, item['id']).claimed_at = utc_now()-timedelta(seconds=60)
            db.commit()
        renewed = self.welcome()
        self.assertNotEqual(renewed['claim_token'], item['claim_token'])
        prefix = f"/api/internal/clawbot/welcome/{item['id']}"
        self.post(prefix+'/start', {'claim_token': renewed['claim_token']})
        with self.sessions() as db:
            db.get(ClawbotWelcome, item['id']).started_at = utc_now()-timedelta(seconds=150)
            db.commit()
        self.assertIsNone(self.welcome())
        with self.sessions() as db:
            self.assertEqual(db.get(ClawbotWelcome, item['id']).status, 'unknown')

    def test_guide_command_can_be_retrieved_without_creating_tasks(self):
        with patch.dict('os.environ', {'PUBLIC_BASE_URL':'https://example.com'}):
            result = self.send(content='使用指南')
        self.assertEqual(result.status_code, 200)
        self.assertIn('https://example.com/guide', result.json()['reply'])
        self.assertIn('不会自动修改微信资料', result.json()['reply'])
        with self.sessions() as db:
            self.assertEqual(db.scalars(select(Task)).all(), [])

    def test_start_repeat_claim_serial_and_other_invite_cannot_view_qr(self):
        first, headers, _ = self.started()
        self.assertEqual(self.post("/api/join/start", headers=headers).json()["state"], "starting")
        _, other, _ = self.new()
        self.assertEqual(self.post("/api/join/start", headers=other).json()["state"], "queued")
        self.assertIsNone(self.post("/api/internal/clawbot/enrollment/claim").json()["job"])
        self.post(f"/api/internal/clawbot/enrollment/{first['id']}/update",
                  {"lease":first["lease"],"state":"awaiting_scan", "qr_image":"data:image/png;base64,YQ=="})
        self.assertIsNotNone(self.client.get("/api/join/status", headers=headers).json()["qr_image"])
        self.assertIsNone(self.client.get("/api/join/status", headers=other).json()["qr_image"])

    def test_public_payload_cannot_register_identity(self):
        _, headers, _ = self.new()
        result = self.post("/api/join/complete", {"account_id":"attack", "sender_id":"attack"}, headers=headers)
        self.assertEqual(result.status_code, 404)
        self.assertEqual(load_bindings(), [fixtures.BINDINGS[0]])
        self.assertEqual(self.post("/api/join/start", {"consent":False}, headers=headers).status_code,422)
        self.assertEqual(self.post("/api/join/start", {"consent":True,"sender_id":"attack"}, headers=headers).status_code,422)

    def test_same_sender_across_two_accounts_still_has_separate_owner(self):
        self.path.write_text(json.dumps([fixtures.BINDINGS[0], {"account_id":"bot-two","sender_id":"Alice"}]), encoding="utf-8")
        self.assertEqual(self.send().status_code,200)
        self.assertEqual(self.send(account="bot-two", mid="same", content="三分钟后提醒我写周报").status_code,200)
        with self.sessions() as db:
            tasks = db.scalars(select(Task)).all()
            self.assertEqual(len(tasks),2); self.assertNotEqual(tasks[0].owner_id,tasks[1].owner_id)

    def test_two_reminders_choose_their_exact_account_and_recipient(self):
        job, _, _ = self.started(); self.complete(job)
        self.send(); self.send(account="bot-two",user="Bob")
        with self.sessions() as db:
            for task in db.scalars(select(Task)).all():
                task.due_at = clawbot_delivery.local_now() - timedelta(seconds=1)
            db.commit()
        with patch("app.scheduler.SessionLocal",self.sessions): scheduler.check_due_tasks()
        first = self.post("/api/internal/clawbot/outbox/claim").json()["delivery"]
        second = self.post("/api/internal/clawbot/outbox/claim").json()["delivery"]
        self.assertEqual({(v["account_id"],v["sender_id"]) for v in (first,second)}, {("bot","Alice"),("bot-two","Bob")})

    def test_wrong_lease_expiry_and_cancel_cannot_admit(self):
        job, headers, _ = self.started()
        wrong = {**job, "lease":"b"*64}
        self.assertEqual(self.complete(wrong).status_code, 403)
        self.post("/api/join/cancel", headers=headers)
        self.assertEqual(self.complete(job).status_code, 409)
        self.assertEqual(load_bindings(), [fixtures.BINDINGS[0]])

    def test_expired_worker_not_reclaimed_and_qr_cleared(self):
        job, headers, _ = self.started()
        with self.sessions() as db:
            row = db.get(ClawbotInvitation, job["id"])
            row.lease_until = utc_now() - timedelta(seconds=1)
            row.qr_image = "data:image/png;base64,YQ=="
            db.commit()
        self.assertIsNone(self.post("/api/internal/clawbot/enrollment/claim").json()["job"])
        self.assertEqual(self.complete(job).status_code, 409)
        self.assertIsNone(self.client.get("/api/join/status", headers=headers).json()["qr_image"])

    def test_expired_invite_rejected(self):
        job, headers, _ = self.new()
        with self.sessions() as db:
            db.get(ClawbotInvitation, job).expires_at = utc_now() - timedelta(seconds=1)
            db.commit()
        self.assertEqual(self.client.get("/api/join/status", headers=headers).status_code, 401)

    def test_existing_user_or_account_not_overwritten(self):
        job, _, _ = self.started()
        self.assertEqual(self.complete(job, user="Alice").status_code, 409)
        self.assertEqual(self.complete(job, account="bot").status_code, 409)
        self.assertEqual(load_bindings(), [fixtures.BINDINGS[0]])

    def test_capacity_and_worker_readiness_fail_closed(self):
        _, headers, _ = self.new()
        with patch.object(enrollment, "_worker_seen", None):
            self.assertEqual(self.post("/api/join/start", headers=headers).status_code, 503)
        with patch.dict("os.environ", {"CLAWBOT_MAX_USERS":"1"}):
            self.assertEqual(self.post("/api/join/start", headers=headers).status_code, 409)

    def test_pairing_code_strict_single_consumption_not_in_public_view(self):
        job, headers, _ = self.started()
        prefix = f"/api/internal/clawbot/enrollment/{job['id']}"
        self.assertEqual(self.post("/api/join/code", {"code":"1234"}, headers=headers).status_code, 409)
        self.post(prefix+"/update", {"lease":job["lease"],"state":"needs_code"})
        self.assertEqual(self.post("/api/join/code", {"code":"hi"}, headers=headers).status_code, 422)
        self.assertEqual(self.post("/api/join/code", {"code":"1234","sender_id":"attack"}, headers=headers).status_code, 422)
        self.assertEqual(self.post("/api/join/code", {"code":"1234"}, headers=headers).status_code, 200)
        self.assertEqual(self.post("/api/join/code", {"code":"1234"}, headers=headers).status_code, 409)
        self.assertNotIn("1234", self.client.get("/api/join/status", headers=headers).text)
        self.assertEqual(self.post(prefix+"/poll", {"lease":job["lease"]}).json()["code"], "1234")
        self.assertIsNone(self.post(prefix+"/poll", {"lease":job["lease"]}).json()["code"])

    def test_two_accounts_same_message_id_tasks_history_and_schedule_isolated(self):
        job, _, _ = self.started(); self.complete(job)
        self.assertEqual(self.send().status_code, 200)
        self.assertEqual(self.send(account="bot-two", user="Bob", content="三分钟后提醒我写周报").status_code, 200)
        result = self.send(account="bot-two", user="Bob", mid="query", content="查询待办")
        self.assertIn("写周报", result.json()["reply"]); self.assertNotIn("喝水", result.json()["reply"])
        with self.sessions() as db:
            tasks = db.scalars(select(Task).order_by(Task.id)).all()
            self.assertEqual(len(tasks), 2); self.assertNotEqual(tasks[0].owner_id, tasks[1].owner_id)
            link = issue_schedule_link(db, tasks[1].owner_id, "https://example.com")
        result = self.client.post(f"/api/schedule/tasks/{tasks[0].id}/complete",
                                  headers={"Authorization":"Bearer "+link.split("access=")[1]})
        self.assertEqual(result.status_code, 404)

    def test_revoke_blocks_chat_schedule_and_queued_reminder_preserves_tasks(self):
        job, headers, _ = self.started(); self.complete(job)
        self.send(account="bot-two", user="Bob")
        owner = clawbot_owner("bot-two","Bob")
        with self.sessions() as db:
            link = issue_schedule_link(db, owner, "https://example.com")
            task = db.scalar(select(Task)); task.due_at = clawbot_delivery.local_now() - timedelta(seconds=1)
            db.commit()
        with patch("app.scheduler.SessionLocal", self.sessions): scheduler.check_due_tasks()
        item = self.post("/api/internal/clawbot/outbox/claim").json()["delivery"]
        self.assertEqual(item["sender_id"], "Bob")
        self.assertEqual(self.post("/api/join/revoke", headers=headers).json()["state"], "revoked")
        self.assertEqual(self.send(account="bot-two", user="Bob", mid="new").status_code, 403)
        self.assertEqual(self.client.get("/api/schedule/tasks",params={"start":"2026-10-01","end":"2026-11-01"},
            headers={"Authorization":"Bearer "+link.split("access=")[1]}).status_code, 401)
        self.assertEqual(self.post(f"/api/internal/clawbot/outbox/{item['id']}/start", {"claim_token":item["claim_token"]}).status_code, 409)
        with self.sessions() as db: self.assertIsNotNone(db.scalar(select(Task)))
        self.assertEqual(load_bindings(), [fixtures.BINDINGS[0]])

    def test_revoke_all_valid_empty_allowlist_no_fallback(self):
        self.post("/api/internal/clawbot/enrollment/members/"+clawbot_owner("bot","Alice")+"/revoke")
        self.assertEqual(load_bindings(), [])
        self.assertEqual(self.send().status_code, 403)
        self.assertEqual(self.client.get("/api/internal/clawbot/health", headers=self.admin).json()["bindings"], 0)

    def test_malformed_allowlist_fail_closed_and_page_csp(self):
        self.path.write_text(json.dumps([fixtures.BINDINGS[0],fixtures.BINDINGS[0]]), encoding="utf-8")
        self.assertEqual(self.send().status_code, 503)
        response = join_page_response()
        self.assertIn("sha256-", response.headers["content-security-policy"])
        self.assertNotIn("unsafe-inline", response.headers["content-security-policy"])
        self.assertIn("history.replaceState", response.body.decode())
