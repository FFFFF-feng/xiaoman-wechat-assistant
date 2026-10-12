"""主动提醒和手机日程的离线状态机回归，不调用微信。"""
import json
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import clawbot_delivery, scheduler
from app.local_gateway import create_gateway
from app.models import ClawbotDelivery, Reminder, Task
from app.schedule_access import public_base_url
import test_clawbot_bridge as bridge_fixtures

TOKEN = bridge_fixtures.TOKEN


class ClawbotDeliveryTests(TestCase):
    send = bridge_fixtures.ClawbotBridgeTests.send

    def setUp(self):
        bridge_fixtures.ClawbotBridgeTests.setUp(self)
        flag = patch.dict("os.environ", {"CLAWBOT_REMINDERS_ENABLED": "true", "CLAWBOT_REMINDER_MAX_LATENESS_SECONDS": "300"})
        flag.start()
        self.addCleanup(flag.stop)
        self.headers = {"Authorization": f"Bearer {TOKEN}"}

    def queue(self, seconds=5):
        response = self.send()
        self.assertEqual(response.status_code, 200)
        self.assertIn("保持电脑", response.json()["reply"])
        with self.sessions() as db:
            db.scalar(select(Task)).due_at = clawbot_delivery.local_now() - timedelta(seconds=seconds)
            db.commit()
        with patch("app.scheduler.SessionLocal", self.sessions), patch.object(scheduler.adapter, "send") as mock_send:
            scheduler.check_due_tasks()
            scheduler.check_due_tasks()
            mock_send.assert_not_called()

    def claim(self):
        response = self.client.post("/api/internal/clawbot/outbox/claim", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["delivery"]

    def start(self, item):
        return self.client.post(f"/api/internal/clawbot/outbox/{item['id']}/start", headers=self.headers,
                                json={"claim_token": item["claim_token"]})

    def result(self, item, outcome="accepted", **fields):
        return self.client.post(f"/api/internal/clawbot/outbox/{item['id']}/result", headers=self.headers,
            json={"claim_token": item["claim_token"], "outcome": outcome, **fields})

    def test_queue_is_durable_unique_and_platform_acceptance_is_not_delivery(self):
        self.queue()
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(ClawbotDelivery)), 1)
            self.assertEqual(db.scalar(select(Reminder)).status, "queued")
        item = self.claim()
        self.assertEqual(item["sender_id"], "Alice")
        self.assertIsNone(self.claim())
        self.assertEqual(self.start(item).status_code, 200)
        self.assertEqual(self.start(item).status_code, 409)
        self.assertEqual(self.result(item, message_id="platform-test").status_code, 200)
        self.assertEqual(self.result(item, message_id="platform-test").status_code, 200)
        self.assertIsNone(self.claim())
        with self.sessions() as db:
            reminder = db.scalar(select(Reminder))
            self.assertEqual(reminder.status, "accepted")
            self.assertEqual(reminder.channel, "clawbot")
            self.assertEqual(reminder.external_message_id, "platform-test")
            self.assertIsNotNone(reminder.sent_at)

    def test_expired_claim_before_start_can_recover_but_old_lease_is_rejected(self):
        self.queue()
        first = self.claim()
        with self.sessions() as db:
            db.scalar(select(ClawbotDelivery)).claimed_at -= timedelta(seconds=46)
            db.commit()
        second = self.claim()
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["claim_token"], second["claim_token"])
        self.assertEqual(self.start(first).status_code, 409)
        self.assertEqual(self.start(second).status_code, 200)

    def test_expired_sending_is_unknown_never_automatically_retried(self):
        self.queue()
        item = self.claim()
        self.start(item)
        with self.sessions() as db:
            db.scalar(select(ClawbotDelivery)).started_at -= timedelta(seconds=121)
            db.commit()
        self.assertIsNone(self.claim())
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(Reminder)).status, "unknown")
        # 同一次发送的迟到受理结果可以回写，不能重新发送。
        self.assertEqual(self.result(item, message_id="late-receipt").status_code, 200)
        self.assertIsNone(self.claim())

    def test_cancel_or_reschedule_before_send_prevents_platform_call(self):
        for mode in ("cancelled", "snoozed"):
            with self.subTest(mode=mode):
                if mode == "cancelled":
                    self.queue()
                item = self.claim()
                with self.sessions() as db:
                    task = db.scalar(select(Task))
                    task.status = mode
                    if mode == "snoozed":
                        task.due_at = clawbot_delivery.local_now() + timedelta(hours=1)
                    db.commit()
                self.assertEqual(self.start(item).status_code, 409)
                # 为下一个分支重新准备一条未开始的领取，不发送任何微信。
                if mode == "cancelled":
                    with self.sessions() as db:
                        db.scalar(select(Task)).status = "open"
                        db.scalar(select(ClawbotDelivery)).status = "pending"
                        db.commit()

    def test_old_due_task_is_missed_not_mass_sent_on_restart(self):
        self.queue(seconds=301)
        self.assertIsNone(self.claim())
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(Reminder)).status, "missed")
            self.assertEqual(db.scalar(select(func.count()).select_from(ClawbotDelivery)), 0)

    def test_removed_binding_is_blocked_and_owner_is_never_guessed(self):
        self.queue()
        with patch("app.clawbot_router.load_bindings", return_value=[{"account_id": "other", "sender_id": "Alice"}]):
            self.assertIsNone(self.claim())
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(Reminder)).error_code, "binding_removed")

    def test_result_requires_auth_lease_and_platform_message_identity(self):
        self.queue()
        item = self.claim()
        self.assertEqual(self.client.post("/api/internal/clawbot/outbox/claim").status_code, 401)
        self.assertEqual(self.result(item, message_id="too-early").status_code, 409)
        self.start(item)
        self.assertEqual(self.result(item).status_code, 422)
        invalid = {**item, "claim_token": "0" * 64}
        self.assertEqual(self.result(invalid, message_id="fake").status_code, 409)
        self.assertEqual(self.result(item, "unknown", error_code="network_uncertain").status_code, 200)
        self.assertIsNone(self.claim())


class ScheduleTunnelTests(TestCase):
    def test_schedule_only_gateway_blocks_callback_and_internal_endpoints(self):
        import httpx
        requests = []
        def upstream(request):
            requests.append(request)
            return httpx.Response(200, text="page", headers={"content-type": "text/html"})
        with TestClient(create_gateway(18083, schedule_only=True, transport=httpx.MockTransport(upstream))) as client:
            for path in ("/api/channels/wechat/callback", "/api/internal/clawbot/outbox/claim", "/api/internal/clawbot/chat", "/health", "/api/chat", "/api/tasks", "/docs"):
                self.assertEqual(client.get(path).status_code, 404)
                self.assertEqual(client.post(path).status_code, 404)
            self.assertEqual(requests, [])
            response = client.get("/schedule")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
            self.assertEqual(requests[0].url.port, 18083)

    def test_dynamic_url_requires_live_heartbeat_and_valid_quick_tunnel_host(self):
        from starlette.requests import Request
        request = Request({"type": "http", "headers": [(b"host", b"127.0.0.1")]})
        now = datetime.now(timezone.utc)
        with patch.dict("os.environ", {"PUBLIC_BASE_URL": "", "CLAWBOT_ENABLED": "true", "CLAWBOT_PUBLIC_URL_FILE": "private-state"}):
            for url, ready, updated, expected in (
                ("https://example-test.trycloudflare.com", True, now, "https://example-test.trycloudflare.com"),
                ("https://example-test.trycloudflare.com", False, now, None),
                ("https://example-test.trycloudflare.com", True, now - timedelta(seconds=61), None),
                ("https://attacker.invalid", True, now, None),
                ("https://api.trycloudflare.com", True, now, None),
                ("http://example-test.trycloudflare.com", True, now, None),
                ("https://example-test.trycloudflare.com/private", True, now, None),
            ):
                with patch("pathlib.Path.read_text", return_value=json.dumps({"url": url, "ready": ready, "updated_at": updated.isoformat()})):
                    self.assertEqual(public_base_url(request), expected)
