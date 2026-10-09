"""离线回归：所有微信 HTTP 发送均使用 mock，不调用真实公众号接口。"""
import base64
import hashlib
import json
import os
import struct
from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import patch
from xml.etree.ElementTree import Element, SubElement, fromstring, tostring
from zoneinfo import ZoneInfo

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import database, scheduler, wechat_router
from app.adapters.wechat_official import (SUBSCRIBE_MESSAGE_URL, TEMPLATE_MESSAGE_URL,
                                          WechatAPIError, WeChatOfficialAdapter, wechat_adapter,
                                          wechat_channel_status)
from app.chat_service import _execute_command
from app.database import Base
from app.models import (Reminder, Task, WechatDeliveryReceipt, WechatMessage,
                        WechatSubscription, WechatUser)
from app.parser import ParsedCommand
from app.schedule_router import router as schedule_router
from app.schedule_access import schedule_owner
from app.schedule_access import issue_schedule_link
from app.wechat_crypto import WechatCrypto, WechatCryptoError
from app.wechat_notifications import (NotificationBlocked, apply_receipt, handle_event, note_inbound,
                                     owner_for, plan_reminder, send_customer, send_reminder)


KEY = "A" * 43  # 官方公开示例的测试密钥，不是生产配置。
APP_ID = "wxba5fad812f8e6fb9"
NOW = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None, microsecond=0)
ENV = {"WECHAT_ENABLED": "true", "WECHAT_ACCOUNT_MODE": "official", "WECHAT_ENCRYPTION_MODE": "safe",
       "WECHAT_ENCODING_AES_KEY": KEY, "WECHAT_APP_ID": APP_ID, "WECHAT_APP_SECRET": "fake-test-secret",
       "WECHAT_TOKEN": "AAAAA", "WECHAT_ORIGINAL_ID": "gh_test", "WECHAT_REPLY_MODE": "passive",
       "WECHAT_REMINDER_MODE": "customer", "WECHAT_TEMPLATE_ID": "", "WECHAT_TEMPLATE_DATA": "",
       "WECHAT_SUBSCRIBE_ID": "", "WECHAT_SUBSCRIBE_DATA": "", "MODEL_ENABLED": "false",
       "PUBLIC_BASE_URL": "https://example.com", "TIMEZONE": "Asia/Shanghai"}


def xml_message(msg_id="1", msg_type="text", content="两分钟后提醒我喝水", event=None, **fields):
    root = Element("xml")
    values = {"ToUserName": "gh_test", "FromUserName": "test-user", "CreateTime": str(int(
        NOW.replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())), "MsgType": msg_type}
    if msg_type == "text":
        values.update({"MsgId": msg_id, "Content": content})
    if event:
        values["Event"] = event
    values.update(fields)
    for key, value in values.items():
        if isinstance(value, Element):
            root.append(value)
        else:
            SubElement(root, key).text = str(value)
    return tostring(root, encoding="utf-8")


def subscription_event(status="accept", event="subscribe_msg_popup_event"):
    container = Element("SubscribeMsgPopupEvent" if event == "subscribe_msg_popup_event" else "SubscribeMsgChangeEvent")
    item = SubElement(container, "List")
    SubElement(item, "TemplateId").text = "sub-test"
    SubElement(item, "SubscribeStatusString").text = status
    return xml_message(msg_type="event", event=event, container=container)


class CryptoTests(TestCase):
    def setUp(self):
        self.crypto = WechatCrypto("AAAAA", KEY, APP_ID)

    def test_official_published_decryption_vector(self):
        # 独立参考：微信官方 encryption.html 公布的密文/签名，不是自加密后自解密。
        ciphertext = ("+qdx1OKCy+5JPCBFWw70tm0fJGb2Jmeia4FCB7kao+/Q5c/ohsOzQHi8khUOb05JCpj0JB4RvQMkUyus8TPx"
                      "LKJGQqcvZqzDpVzazhZv6JsXUnnR8XGT740XgXZUXQ7vJVnAG+tE8NUd4yFyjPy7GgiaviNrlCTj+l5kdfMuF"
                      "UPpRSrfMZuMcp3Fn2Pede2IuQrKEYwKSqFIZoNqJ4M8EajAsjLY2km32IIjdf8YL/P50F7mStwntrA2cPDrM1"
                      "kb6mOcfBgRtWygb3VIYnSeOBrebufAlr7F9mFUPAJGj04=")
        self.assertTrue(self.crypto.verify("046e02f8204d34f8ba5fa3b1db94908f3df2e9b3", ciphertext,
                                           "1714112445", "415670741"))
        payload = json.loads(self.crypto.decrypt(ciphertext))
        self.assertEqual(payload["debug_str"], "hello world")
        self.assertEqual(payload["ToUserName"], "gh_97417a04a28d")

    def test_official_published_encryption_vector(self):
        with patch("app.wechat_crypto.secrets.token_bytes", return_value=b"707722b803182950"):
            ciphertext = self.crypto.encrypt(b'{"demo_resp":"good luck"}')
        self.assertEqual(ciphertext, "ELGduP2YcVatjqIS+eZbp80MNLoAUWvzzyJxgGzxZO/5sAvd070Bs6qrLARC9nVHm48Y4hyRbtzve1L32tmxSQ==")
        self.assertEqual(self.crypto.signature(ciphertext, "1713424427", "415670741"),
                         "1b9339964ed2e271e7c7b6ff2b0ef902fc94dea1")

    def test_unicode_roundtrip_and_reply_signature(self):
        reply = fromstring(self.crypto.reply("喝水 & 休息 <一会>".encode(), "nonce"))
        self.assertTrue(self.crypto.verify(reply.findtext("MsgSignature"), reply.findtext("Encrypt"),
                                           reply.findtext("TimeStamp"), reply.findtext("Nonce")))
        self.assertEqual(self.crypto.decrypt(reply.findtext("Encrypt")).decode(), "喝水 & 休息 <一会>")

    def test_wrong_appid_rejected(self):
        ciphertext = self.crypto.encrypt(b"hello")
        with self.assertRaises(WechatCryptoError):
            WechatCrypto("AAAAA", KEY, "other-app").decrypt(ciphertext)

    def test_invalid_key_and_base64(self):
        for key in ("short", "@" * 43):
            with self.assertRaises(WechatCryptoError):
                WechatCrypto("token", key, APP_ID)
        for ciphertext in ("!not-base64", "", base64.b64encode(b"short").decode()):
            with self.assertRaises(WechatCryptoError):
                self.crypto.decrypt(ciphertext)

    def test_invalid_padding_and_forged_length_rejected(self):
        for plain in (b"x" * 32, b"r" * 16 + struct.pack("!I", 1000) + b"msg" + APP_ID.encode()):
            if len(plain) != 32:
                padding = 32 - len(plain) % 32
                plain += bytes([padding]) * padding
            encryptor = Cipher(algorithms.AES(self.crypto.key), modes.CBC(self.crypto.key[:16])).encryptor()
            ciphertext = base64.b64encode(encryptor.update(plain) + encryptor.finalize()).decode()
            with self.assertRaises(WechatCryptoError):
                self.crypto.decrypt(ciphertext)


class DatabaseCase(TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        for name in ("app.wechat_router.SessionLocal", "app.scheduler.SessionLocal"):
            mock = patch(name, self.factory)
            mock.start()
            self.addCleanup(mock.stop)
        self.db = self.factory()
        self.addCleanup(self.db.close)
        self.clock = patch("app.parser.now_local", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.no_model = patch("app.chat_service.parse_with_model", return_value=None)
        self.no_model.start()
        self.addCleanup(self.no_model.stop)
        send_patch = patch.object(wechat_adapter, "send", return_value=True)
        self.send = send_patch.start()
        self.addCleanup(send_patch.stop)
        notification_patch = patch.object(wechat_adapter, "send_notification", return_value={"errcode": 0, "msgid": 123})
        self.notification = notification_patch.start()
        self.addCleanup(notification_patch.stop)
        self.owner = owner_for("test-user")
        app = FastAPI()
        app.include_router(wechat_router.router)
        app.include_router(schedule_router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        app.dependency_overrides[schedule_owner] = lambda: self.owner
        app.dependency_overrides[database.get_db] = lambda: self.db
        self.crypto = WechatCrypto.from_env()

    def user(self, last_at=NOW, used=0):
        state = WechatUser(app_id=APP_ID, openid="test-user", last_message_at=last_at,
                           active=True, customer_messages_used=used)
        self.db.add(state)
        self.db.commit()
        return state

    def task(self, **kwargs):
        values = {"title": "喝水", "due_at": NOW, "status": "open", "owner_id": self.owner,
                  "wechat_app_id": APP_ID, "reminder_channel": "customer"}
        values.update(kwargs)
        task = Task(**values)
        self.db.add(task)
        self.db.commit()
        return task

    def post(self, body=None, encrypted=True, **params):
        body = body or xml_message()
        query = {"timestamp": "1714112445", "nonce": "n"}
        if encrypted:
            ciphertext = self.crypto.encrypt(body)
            outer = Element("xml")
            SubElement(outer, "ToUserName").text = "gh_test"
            SubElement(outer, "Encrypt").text = ciphertext
            body = tostring(outer)
            query.update({"encrypt_type": "aes", "msg_signature": self.crypto.signature(ciphertext, query["timestamp"], "n")})
        else:
            query["signature"] = hashlib.sha1("".join(sorted(("AAAAA", query["timestamp"], "n"))).encode()).hexdigest()
        query.update(params)
        return self.client.post("/api/channels/wechat/callback", params=query, content=body)


class CallbackTests(DatabaseCase):
    def test_encrypted_passive_reply_and_dedup(self):
        first = self.post()
        self.assertEqual(first.status_code, 200, first.text)
        root = fromstring(first.content)
        self.assertIsNotNone(root.findtext("Encrypt"))
        reply = fromstring(self.crypto.decrypt(root.findtext("Encrypt")))
        self.assertIn("提醒你", reply.findtext("Content"))
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.db.scalar(select(func.count(Task.id))), 1)
        task = self.db.scalar(select(Task))
        self.assertEqual(task.owner_id, self.owner)
        self.assertEqual(task.wechat_app_id, APP_ID)
        self.assertEqual(task.reminder_channel, "customer")

    def test_safe_mode_rejects_plaintext_downgrade(self):
        self.assertEqual(self.post(encrypted=False).status_code, 403)
        self.assertEqual(self.db.scalar(select(func.count(WechatMessage.id))), 0)

    def test_compatible_and_plain_modes_preserve_sandbox(self):
        for mode in ("compatible", "plain"):
            with patch.dict(os.environ, {"WECHAT_ENCRYPTION_MODE": mode, "WECHAT_ACCOUNT_MODE": "test"}):
                response = self.post(xml_message(msg_id=mode), encrypted=False)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(fromstring(response.content).findtext("MsgType"), "text")

    def test_signature_tamper_is_rejected_before_decrypt(self):
        with patch.object(WechatCrypto, "decrypt", side_effect=AssertionError("should not decrypt")):
            self.assertEqual(self.post(msg_signature="invalid").status_code, 403)

    def test_encrypted_get_verification(self):
        echo = self.crypto.encrypt(b"echo-string")
        response = self.client.get("/api/channels/wechat/callback", params={
            "timestamp": "t", "nonce": "n", "echostr": echo,
            "msg_signature": self.crypto.signature(echo, "t", "n")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "echo-string")

    def test_safe_mode_standard_get_handshake_is_allowed(self):
        signature = hashlib.sha1("".join(sorted(("AAAAA", "t", "n"))).encode()).hexdigest()
        response = self.client.get("/api/channels/wechat/callback", params={
            "signature": signature, "timestamp": "t", "nonce": "n", "echostr": "echo"})
        self.assertEqual(response.text, "echo")

    def test_wrong_receiver_invalid_xml_disabled_and_bad_time(self):
        self.assertEqual(self.post(xml_message(ToUserName="gh_other")).status_code, 400)
        self.assertEqual(self.post(b'<!DOCTYPE xml [<!ENTITY x "bad">]><xml>&x;</xml>').status_code, 400)
        self.assertEqual(self.post(xml_message(CreateTime="invalid")).status_code, 400)
        with patch.dict(os.environ, {"WECHAT_ENABLED": "false"}):
            self.assertEqual(self.post().status_code, 503)

    def test_event_ack_never_sends_chat_or_opens_48h_window(self):
        response = self.post(xml_message(msg_type="event", event="subscribe"))
        self.assertEqual(response.text, "success")
        self.send.assert_not_called()
        self.assertIsNone(self.db.scalar(select(WechatUser)).last_message_at)
        with self.assertRaises(NotificationBlocked):
            send_customer(self.db, "test-user", "hello", NOW)

    def test_subscription_accept_is_deduplicated_and_reject_revokes(self):
        for _ in range(2):
            self.assertEqual(self.post(subscription_event()).text, "success")
        self.assertEqual(self.db.scalar(select(WechatSubscription)).credits, 1)
        self.assertEqual(self.post(subscription_event("reject", "subscribe_msg_change_event")).text, "success")
        self.db.expire_all()
        self.assertEqual(self.db.scalar(select(WechatSubscription)).credits, 0)
        self.send.assert_not_called()

    def test_async_send_failure_is_recorded_not_silently_processed(self):
        self.send.side_effect = WechatAPIError(45015, "reply timeout")
        with patch.dict(os.environ, {"WECHAT_REPLY_MODE": "async"}), self.assertLogs("app.wechat_router", level="ERROR"):
            response = self.post()
        self.assertEqual(response.text, "success")
        row = self.db.scalar(select(WechatMessage))
        self.assertEqual(row.status, "failed")
        self.assertIn("45015", row.error)

    def test_async_encrypted_callback_ack_and_send_once(self):
        with patch.dict(os.environ, {"WECHAT_REPLY_MODE": "async"}):
            self.assertEqual(self.post().text, "success")
            self.assertEqual(self.post().text, "success")
        self.send.assert_called_once()

    def test_same_msgid_is_scoped_to_appid(self):
        with patch.dict(os.environ, {"WECHAT_ENCRYPTION_MODE": "plain", "WECHAT_ACCOUNT_MODE": "test"}):
            self.assertEqual(self.post(encrypted=False).status_code, 200)
            with patch.dict(os.environ, {"WECHAT_APP_ID": "wx-other"}):
                self.assertEqual(self.post(encrypted=False).status_code, 200)
        self.assertEqual(self.db.scalar(select(func.count(WechatMessage.id))), 2)

    def test_oversized_request_is_rejected(self):
        response = self.client.post("/api/channels/wechat/callback", params={"timestamp": "t", "nonce": "n"},
                                    content=b"x" * (256 * 1024 + 1))
        self.assertEqual(response.status_code, 413)

    def test_subscription_old_accept_cannot_undo_newer_rejection(self):
        reject = wechat_router.parse_wechat_xml(subscription_event("reject", "subscribe_msg_change_event"))
        handle_event(self.db, reject, APP_ID)
        self.db.commit()
        older_accept = wechat_router.parse_wechat_xml(subscription_event())
        older_accept["CreateTime"] = str(int(older_accept["CreateTime"]) - 1)
        handle_event(self.db, older_accept, APP_ID)
        self.db.commit()
        self.assertEqual(self.db.scalar(select(WechatSubscription)).credits, 0)

    def test_calendar_tokens_cannot_cross_app_or_test_account(self):
        link = issue_schedule_link(self.db, self.owner, "https://example.com")
        authorization = "Bearer " + link.split("#access=", 1)[1]
        self.assertEqual(schedule_owner(authorization=authorization, db=self.db), self.owner)
        with patch.dict(os.environ, {"WECHAT_APP_ID": "other-app"}):
            with self.assertRaises(Exception) as result:
                schedule_owner(authorization=authorization, db=self.db)
            self.assertEqual(result.exception.status_code, 401)
        test_link = issue_schedule_link(self.db, "wechat:test-user", "https://example.com")
        with self.assertRaises(Exception) as result:
            schedule_owner(authorization="Bearer " + test_link.split("#access=", 1)[1], db=self.db)
        self.assertEqual(result.exception.status_code, 401)


class ReminderTests(DatabaseCase):
    def test_customer_window_boundary_is_rejected_without_task(self):
        self.user()
        response = _execute_command(ParsedCommand("create", title="周报", due_at=NOW + timedelta(hours=48)),
                                    self.db, self.owner)
        self.assertEqual(response.action, "clarify")
        self.assertIn("48小时", response.reply)
        self.assertEqual(self.db.scalar(select(func.count(Task.id))), 0)

    def test_schedule_snooze_cannot_bypass_window(self):
        self.user()
        task = self.task(due_at=NOW + timedelta(minutes=10))
        response = self.client.post(f"/api/schedule/tasks/{task.id}/snooze",
                                    json={"due_at": (NOW + timedelta(days=3)).isoformat()})
        self.assertEqual(response.status_code, 422, response.text)
        self.db.refresh(task)
        self.assertEqual(task.due_at, NOW + timedelta(minutes=10))

    def test_customer_quota_and_new_message_reset(self):
        state = self.user()
        for _ in range(5):
            send_customer(self.db, "test-user", "hello", NOW)
        with self.assertRaises(NotificationBlocked):
            send_customer(self.db, "test-user", "sixth", NOW)
        self.assertEqual(self.send.call_count, 5)
        payload = wechat_router.parse_wechat_xml(xml_message(CreateTime=str(int(
            (NOW + timedelta(seconds=1)).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()))))
        note_inbound(self.db, payload, APP_ID, NOW + timedelta(seconds=1))
        self.db.commit()
        self.db.refresh(state)
        self.assertEqual(state.customer_messages_used, 0)

    def test_old_retry_does_not_reset_customer_quota(self):
        state = self.user(used=4)
        note_inbound(self.db, wechat_router.parse_wechat_xml(xml_message()), APP_ID, NOW)
        self.db.commit()
        self.db.refresh(state)
        self.assertEqual(state.customer_messages_used, 4)

    def test_pending_reminders_reserve_customer_quota(self):
        self.user(used=4)
        self.task(due_at=NOW + timedelta(minutes=1))
        with self.assertRaises(NotificationBlocked):
            plan_reminder(self.db, self.owner, NOW + timedelta(minutes=2), reserve_reply=False)

    def test_template_channel_allows_cross_window_but_requires_real_configuration(self):
        self.user()
        with patch.dict(os.environ, {"WECHAT_REMINDER_MODE": "template"}):
            with self.assertRaises(NotificationBlocked):
                plan_reminder(self.db, self.owner, NOW + timedelta(days=7))
            with patch.dict(os.environ, {"WECHAT_TEMPLATE_ID": "template-test", "WECHAT_TEMPLATE_DATA":
                                         '{"thing1":{"value":"{title_short}"},"time2":{"value":"{due_at}"}}'}):
                channel, template = plan_reminder(self.db, self.owner, NOW + timedelta(days=7), "周报")
                self.assertEqual((channel, template), ("template", "template-test"))
                task = self.task(reminder_channel=channel, notification_template_id=template)
                send_reminder(self.db, task, "提醒", NOW)
                self.notification.assert_called_once_with("template", "test-user", "template-test", {
                    "thing1": {"value": "喝水"}, "time2": {"value": NOW.strftime("%Y-%m-%d %H:%M")}})

    def test_subscription_requires_consent_and_consumes_only_once(self):
        self.user()
        with patch.dict(os.environ, {"WECHAT_REMINDER_MODE": "subscribe", "WECHAT_SUBSCRIBE_ID": "sub-test",
                                     "WECHAT_SUBSCRIBE_DATA": '{"thing1":{"value":"{title_short}"}}'}):
            with self.assertRaises(NotificationBlocked):
                plan_reminder(self.db, self.owner, NOW + timedelta(days=3))
            handle_event(self.db, wechat_router.parse_wechat_xml(subscription_event()), APP_ID)
            self.db.commit()
            channel, template = plan_reminder(self.db, self.owner, NOW + timedelta(days=3))
            task = self.task(reminder_channel=channel, notification_template_id=template)
            with self.assertRaises(NotificationBlocked):
                plan_reminder(self.db, self.owner, NOW + timedelta(days=4))
            send_reminder(self.db, task, "提醒", NOW)
            with self.assertRaises(NotificationBlocked):
                send_reminder(self.db, task, "重复提醒", NOW)
            self.notification.assert_called_once()
            self.db.expire_all()
            self.assertEqual(self.db.scalar(select(WechatSubscription)).credits, 0)

    def test_unsubscribe_and_account_switch_do_not_send(self):
        state = self.user()
        task = self.task()
        state.active = False
        self.db.commit()
        with self.assertRaises(NotificationBlocked):
            send_reminder(self.db, task, "提醒", NOW)
        state.active = True
        for account in (None, "test-account"):
            task.wechat_app_id = account
            self.db.commit()
            with self.assertRaises(NotificationBlocked):
                send_reminder(self.db, task, "提醒", NOW)
        self.send.assert_not_called()

    def test_template_delivery_and_failure_receipts(self):
        task = self.task(reminder_channel="template")
        reminder = Reminder(task_id=task.id, reminder_time=NOW, channel="template", external_message_id="123", status="accepted")
        self.db.add(reminder)
        self.db.commit()
        self.assertEqual(self.post(xml_message(msg_type="event", event="TEMPLATESENDJOBFINISH", MsgID="123", Status="success")).text, "success")
        self.db.refresh(reminder)
        self.assertEqual(reminder.status, "delivered")
        self.assertIsNotNone(reminder.sent_at)
        other = Reminder(task_id=task.id, reminder_time=NOW + timedelta(seconds=1), channel="template",
                         external_message_id="124", status="accepted")
        self.db.add(other)
        self.db.commit()
        self.post(xml_message(msg_type="event", event="TEMPLATESENDJOBFINISH", MsgID="124", Status="failed:user block"))
        self.db.refresh(other)
        self.assertEqual(other.status, "failed")
        self.assertIn("user block", other.error)
        self.send.assert_not_called()

    def test_receipt_can_arrive_before_api_result_is_saved(self):
        handle_event(self.db, wechat_router.parse_wechat_xml(xml_message(msg_type="event",
            event="TEMPLATESENDJOBFINISH", MsgID="123", Status="success")), APP_ID)
        self.db.commit()
        task = self.task(reminder_channel="template")
        reminder = Reminder(task_id=task.id, reminder_time=NOW, channel="template", external_message_id="123", status="accepted")
        self.db.add(reminder)
        apply_receipt(self.db, reminder, APP_ID)
        self.assertEqual(reminder.status, "delivered")

    def test_scheduler_blocked_and_no_duplicate_attempt(self):
        self.user(last_at=NOW - timedelta(days=3))
        self.task(due_at=NOW - timedelta(seconds=10))
        with self.assertLogs("app.scheduler", level="ERROR"):
            scheduler.check_due_tasks()
        scheduler.check_due_tasks()
        reminder = self.db.scalar(select(Reminder))
        self.assertEqual(reminder.status, "blocked")
        self.assertEqual(reminder.error_code, "customer_window_or_quota")
        self.assertEqual(self.db.scalar(select(func.count(Reminder.id))), 1)
        self.send.assert_not_called()

    def test_scheduler_accepted_does_not_claim_delivery(self):
        self.user()
        self.task(due_at=NOW - timedelta(seconds=1))
        scheduler.check_due_tasks()
        reminder = self.db.scalar(select(Reminder))
        self.assertEqual(reminder.status, "accepted")
        self.assertIsNone(reminder.sent_at)
        self.send.assert_called_once()

    def test_scheduler_offline_lateness_is_missed(self):
        self.user()
        self.task(due_at=NOW - timedelta(days=2))
        with self.assertLogs("app.scheduler", level="WARNING"):
            scheduler.check_due_tasks()
        self.assertEqual(self.db.scalar(select(Reminder)).status, "missed")
        self.send.assert_not_called()

    def test_scheduler_network_failure_is_recorded_and_not_retried(self):
        self.user()
        self.task(due_at=NOW - timedelta(seconds=1))
        self.send.side_effect = RuntimeError("发送微信消息的网络请求失败（ReadTimeout）")
        with self.assertLogs("app.scheduler", level="ERROR"):
            scheduler.check_due_tasks()
        scheduler.check_due_tasks()
        reminder = self.db.scalar(select(Reminder))
        self.assertEqual(reminder.status, "failed")
        self.assertIn("ReadTimeout", reminder.error)
        self.send.assert_called_once()

    def test_rejected_subscription_after_task_creation_blocks_send(self):
        self.user()
        with patch.dict(os.environ, {"WECHAT_REMINDER_MODE": "subscribe", "WECHAT_SUBSCRIBE_ID": "sub-test",
                                     "WECHAT_SUBSCRIBE_DATA": '{"thing1":{"value":"{title_short}"}}'}):
            handle_event(self.db, wechat_router.parse_wechat_xml(subscription_event()), APP_ID)
            self.db.commit()
            channel, template = plan_reminder(self.db, self.owner, NOW + timedelta(days=3))
            task = self.task(reminder_channel=channel, notification_template_id=template)
            handle_event(self.db, wechat_router.parse_wechat_xml(subscription_event("reject", "subscribe_msg_change_event")), APP_ID)
            self.db.commit()
            with self.assertRaises(NotificationBlocked):
                send_reminder(self.db, task, "提醒", NOW)
            self.notification.assert_not_called()


class AdapterAndMigrationTests(TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_template_and_subscribe_use_distinct_official_apis(self):
        adapter = WeChatOfficialAdapter()
        with patch.object(adapter, "_post", return_value={"errcode": 0}) as post:
            adapter.send_notification("template", "user", "id", {"thing1": {"value": "test"}})
            self.assertEqual(post.call_args.args[0], TEMPLATE_MESSAGE_URL)
            adapter.send_notification("subscribe", "user", "id", {"thing1": {"value": "test"}})
            self.assertEqual(post.call_args.args[0], SUBSCRIBE_MESSAGE_URL)
            self.assertNotIn("page", post.call_args.args[1])

    def test_expired_token_only_retries_once(self):
        adapter = WeChatOfficialAdapter()
        with patch.object(adapter, "_post_once", side_effect=[{"errcode": 42001}, {"errcode": 0}]) as post:
            adapter.send("user", "hello")
            self.assertEqual(post.call_count, 2)
            self.assertTrue(post.call_args.kwargs["force_refresh"])

    def test_network_error_does_not_expose_secret_or_retry(self):
        adapter = WeChatOfficialAdapter()
        with patch.object(adapter, "_get_access_token", return_value="private-access-token"), patch(
                "app.adapters.wechat_official.httpx.post", side_effect=httpx.ReadTimeout(
                    "https://api.weixin.qq.com?access_token=private-access-token")) as post:
            with self.assertRaises(RuntimeError) as result:
                adapter.send("user", "hello")
            self.assertNotIn("private-access-token", str(result.exception))
            self.assertEqual(post.call_count, 1)

    def test_health_reports_config_errors_without_keys(self):
        with patch.dict(os.environ, {"WECHAT_ENCODING_AES_KEY": "SECRET-BAD-KEY"}):
            status = wechat_channel_status()
        self.assertEqual(status["encryption"], "invalid")
        self.assertTrue(status["configuration_errors"])
        self.assertNotIn("SECRET-BAD-KEY", repr(status))
        self.assertFalse(status["reminders"]["permission_verified"])

    def test_sqlite_migration_preserves_legacy_data_and_is_repeatable(self):
        engine = create_engine("sqlite://")
        try:
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title VARCHAR(300), due_at DATETIME, status VARCHAR(30), priority VARCHAR(20), created_at DATETIME, updated_at DATETIME)"))
                connection.execute(text("INSERT INTO tasks VALUES (1, '保留旧任务', '2026-10-04 17:00:00', 'open', 'normal', '2026-10-04', '2026-10-04')"))
                connection.execute(text("CREATE TABLE reminders (id INTEGER PRIMARY KEY, task_id INTEGER, reminder_time DATETIME, sent_at DATETIME, status VARCHAR(30))"))
                connection.execute(text("CREATE TABLE wechat_messages (id INTEGER PRIMARY KEY, external_message_id VARCHAR(100), from_user VARCHAR(160), to_user VARCHAR(160), message_type VARCHAR(30), content TEXT, status VARCHAR(30), reply_content TEXT, error TEXT, created_at DATETIME, processed_at DATETIME)"))
            with patch.object(database, "engine", engine):
                database.init_db()
                database.init_db()
            with Session(engine) as db:
                task = db.get(Task, 1)
                self.assertEqual(task.title, "保留旧任务")
                self.assertIsNone(task.wechat_app_id)
                db.add(WechatUser(app_id=APP_ID, openid="user", active=True))
                db.commit()
        finally:
            engine.dispose()
