"""离线验证：不调用微信或百炼，不使用真实凭据。"""
import json
import re
from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import clawbot_router, scheduler
from app.chat_service import _recent_chat_history
from app.clawbot_identity import clawbot_owner
from app.database import Base, get_db
from app.local_gateway import allowed_path
from app.models import ClawbotMessage, Message, Reminder, ScheduleAccess, Task
from app.schedule_access import schedule_owner
from app.schedule_router import router as schedule_router

TOKEN = "a" * 64
BINDINGS = [{"account_id": "bot", "sender_id": "Alice"}, {"account_id": "bot", "sender_id": "Bob"}]


class ClawbotBridgeTests(TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        app = FastAPI()
        app.include_router(clawbot_router.router)
        app.include_router(schedule_router)

        def db_override():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = db_override
        self.client = TestClient(app)
        self.patches = [
            patch.dict("os.environ", {"CLAWBOT_ENABLED": "true", "CLAWBOT_BRIDGE_TOKEN": TOKEN,
                                      "CLAWBOT_REMINDERS_ENABLED": "false", "PUBLIC_BASE_URL": "", "WECHAT_ENABLED": "false"}),
            patch("app.clawbot_router.load_bindings", return_value=BINDINGS),
            patch("app.chat_service.parse_with_model", return_value=None),
            patch.dict(clawbot_router._rate_hits, {}, clear=True),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.client.close)
        self.addCleanup(self.engine.dispose)

    def send(self, content="两分钟后提醒我喝水", user="Alice", mid="1", **extra):
        return self.client.post("/api/internal/clawbot/chat", headers={"Authorization": f"Bearer {TOKEN}"},
                                json={"account_id": "bot", "sender_id": user, "message_id": mid, "content": content, **extra})

    def test_authentication_disabled_and_public_gateway(self):
        self.assertEqual(self.client.get("/api/internal/clawbot/health").status_code, 401)
        self.assertEqual(self.client.get("/api/internal/clawbot/health", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        with patch.dict("os.environ", {"CLAWBOT_ENABLED": "false"}):
            self.assertEqual(self.send().status_code, 503)
        for path in ("/api/internal/clawbot/chat", "/api/internal/clawbot/health"):
            for method in ("GET", "POST"):
                self.assertFalse(allowed_path(path, method))

    def test_validation_binding_and_limits(self):
        self.assertEqual(self.send(owner_id="wechat:other").status_code, 422)
        self.assertEqual(self.send(user="alice").status_code, 403)
        self.assertEqual(self.send(" ").status_code, 422)
        self.assertEqual(self.send("x" * 9000).status_code, 422)
        self.assertEqual(self.send("x" * 33000).status_code, 413)

    def test_task_owner_dedup_and_no_global_chat(self):
        first = self.send()
        self.assertEqual(first.status_code, 200, first.text)
        self.assertIn("自动提醒暂不可用", first.json()["reply"])
        self.assertEqual(self.send().json(), first.json())
        self.assertEqual(self.send("两分钟后提醒我吃饭").status_code, 409)
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Task)), 1)
            task = db.scalar(select(Task))
            self.assertEqual(task.owner_id, clawbot_owner("bot", "Alice"))
            self.assertEqual(task.reminder_channel, "clawbot_pending")
            self.assertEqual(db.scalar(select(func.count()).select_from(Message)), 0)

    def test_confirmed_task_is_not_created_again_by_date_only_followup(self):
        self.send()
        self.send("今天", mid="2")
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Task)), 1)

    def test_brand_query_does_not_claim_wechat_contact_was_renamed(self):
        result = self.send("小满资料")
        self.assertEqual(result.status_code, 200)
        self.assertIn("手动修改头像和备注名", result.json()["reply"])
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Task)), 0)

    def test_new_message_rate_limit_does_not_reject_cached_delivery(self):
        with patch("app.clawbot_router.RATE_LIMIT", 1):
            self.assertEqual(self.send().status_code, 200)
            self.assertEqual(self.send().status_code, 200)
            self.assertEqual(self.send("查询待办", mid="2").status_code, 429)

    def test_users_and_history_are_isolated(self):
        self.send("两分钟后提醒我私人任务A")
        self.send("两分钟后提醒我私人任务B", user="Bob")
        alice = self.send("查询待办", mid="2")
        bob = self.send("查询待办", user="Bob", mid="2")
        self.assertIn("私人A", alice.json()["reply"])
        self.assertNotIn("私人B", alice.json()["reply"])
        self.assertIn("私人B", bob.json()["reply"])
        self.assertNotIn("私人A", bob.json()["reply"])
        self.send("取消私人任务B", mid="3")
        with self.sessions() as db:
            other = db.scalar(select(Task).where(Task.owner_id == clawbot_owner("bot", "Bob")))
            self.assertEqual(other.status, "open")
            history = json.dumps(_recent_chat_history(db, clawbot_owner("bot", "Alice")), ensure_ascii=False)
            self.assertNotIn("提醒我私人任务B", history)

    def test_processing_and_failed_records_are_not_reexecuted(self):
        self.send()
        with self.sessions() as db:
            row = db.scalar(select(ClawbotMessage))
            row.status = "processing"
            db.commit()
        self.assertEqual(self.send().status_code, 409)
        with self.sessions() as db:
            db.scalar(select(ClawbotMessage)).status = "failed"
            db.commit()
        self.assertEqual(self.send().status_code, 409)

    def test_partial_commit_failure_requires_manual_query_not_auto_retry(self):
        from app.chat_service import handle_chat_message

        def fail_after_commit(content, db, conversation_id):
            handle_chat_message(content, db, conversation_id, use_model=False)
            raise RuntimeError("simulated crash after task commit")

        with patch("app.clawbot_router.handle_chat_message", side_effect=fail_after_commit):
            self.assertEqual(self.send().status_code, 502)
        self.assertEqual(self.send().status_code, 409)
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Task)), 1)
            self.assertEqual(db.scalar(select(ClawbotMessage)).status, "failed")
    def test_due_reminder_is_blocked_not_mock_sent_or_global_history(self):
        self.send()
        with self.sessions() as db:
            db.scalar(select(Task)).due_at = datetime.now() - timedelta(minutes=1)
            db.commit()
        with patch("app.scheduler.SessionLocal", self.sessions), patch.object(scheduler.adapter, "send") as send:
            scheduler.check_due_tasks()
            send.assert_not_called()
        with self.sessions() as db:
            reminder = db.scalar(select(Reminder))
            self.assertEqual(reminder.status, "blocked")
            self.assertEqual(reminder.error_code, "clawbot_not_configured")
            self.assertIsNone(reminder.sent_at)
            self.assertEqual(db.scalar(select(func.count()).select_from(Message)), 0)

    def test_schedule_no_public_url_and_redacted_authorization_link(self):
        self.assertIn("查询待办", self.send("打开日程").json()["reply"])
        with patch.dict("os.environ", {"PUBLIC_BASE_URL": "https://example.com", "WECHAT_ACCOUNT_MODE": "official"}):
            reply = self.send("打开日程", mid="2").json()["reply"]
            match = re.search(r"\[点击打开我的日程\]\((https://example\.com/schedule#access=([A-Za-z0-9_-]+))\)", reply)
            self.assertIsNotNone(match, "ClawBot schedule replies must use the phone-verified Markdown link format")
            token = match[2]
            with self.sessions() as db:
                record = db.scalar(select(ClawbotMessage).where(ClawbotMessage.content == "打开日程").order_by(ClawbotMessage.id.desc()))
                self.assertNotIn(token, record.response_json)
                self.assertNotIn(token, record.reply_content)
                access = db.scalar(select(ScheduleAccess))
                self.assertNotEqual(token, access.token_hash)
                self.assertEqual(schedule_owner(f"Bearer {token}", db), clawbot_owner("bot", "Alice"))
            # 不允许 Alice 的链接修改 Bob 的任务。
            task = self.send(user="Bob").json()["task"]
            response = self.client.post(f"/api/schedule/tasks/{task['id']}/cancel", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(response.status_code, 404)
            repeated = self.send("打开日程", mid="2").json()["reply"]
            self.assertIn("[点击打开我的日程](https://example.com/schedule#access=", repeated)
