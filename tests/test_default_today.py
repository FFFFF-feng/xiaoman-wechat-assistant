from datetime import datetime
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.chat_service import handle_chat_message
from app.chat_service import _reconcile_commands
from app.database import Base
from app.models import Task
from app.parser import ParsedCommand, parse_command, parse_time_followup


NOW = datetime(2026, 9, 30, 14, 12)


class DefaultTodayTests(TestCase):
    def setUp(self):
        self.clock = patch("app.parser.now_local", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_create_without_date_defaults_to_today(self):
        command = parse_command("提醒我下午五点洗澡收拾出门")
        self.assertEqual(command.action, "create")
        self.assertEqual(command.due_at, datetime(2026, 9, 30, 17, 0))
        self.assertEqual(command.title, "洗澡收拾出门")

    def test_past_time_does_not_roll_to_tomorrow(self):
        command = parse_command("下午一点提醒我洗澡")
        self.assertEqual(command.action, "clarify")
        self.assertIn("今天这个时间已经过去了", command.message)

    def test_explicit_tomorrow_is_preserved(self):
        command = parse_command("明天下午五点提醒我洗澡")
        self.assertEqual(command.action, "create")
        self.assertEqual(command.due_at, datetime(2026, 10, 1, 17, 0))

    def test_missing_time_still_asks(self):
        command = parse_command("提醒我洗澡")
        self.assertEqual(command.action, "clarify")
        self.assertIn("几点", command.message)

    def test_snooze_without_date_defaults_to_today(self):
        command = parse_command("把周报延后到下午五点")
        self.assertEqual(command.action, "snooze")
        self.assertEqual(command.due_at, datetime(2026, 9, 30, 17, 0))

    def test_time_only_followup_defaults_to_today(self):
        command = parse_time_followup("下午五点", "提醒我洗澡")
        self.assertIsNotNone(command)
        self.assertEqual(command.action, "create")
        self.assertEqual(command.due_at, datetime(2026, 9, 30, 17, 0))

    def test_followup_does_not_duplicate_confirmed_task(self):
        command = parse_time_followup(
            "今天",
            "提醒我下午五点洗澡",
            "收到，我会在 09月30日 17:00 提醒你：洗澡。",
        )
        self.assertIsNone(command)

    def test_old_model_date_question_does_not_block_today(self):
        text = "提醒我下午五点洗澡"
        model = ParsedCommand("clarify", message="我需要明确的日期和时间")
        command = _reconcile_commands(text, model, parse_command(text), None)
        self.assertEqual(command.action, "create")
        self.assertEqual(command.due_at, datetime(2026, 9, 30, 17, 0))

    def test_model_cannot_turn_implicit_date_into_tomorrow(self):
        text = "提醒我下午五点洗澡"
        model = ParsedCommand("create", title="洗澡", due_at=datetime(2026, 10, 1, 17, 0))
        command = _reconcile_commands(text, model, parse_command(text), None)
        self.assertEqual(command.action, "create")
        self.assertEqual(command.due_at, datetime(2026, 9, 30, 17, 0))

    def test_model_cannot_create_past_implicit_time(self):
        text = "下午一点提醒我洗澡"
        model = ParsedCommand("create", title="洗澡", due_at=datetime(2026, 10, 1, 13, 0))
        command = _reconcile_commands(text, model, parse_command(text), None)
        self.assertEqual(command.action, "clarify")
        self.assertIn("今天这个时间已经过去了", command.message)

    def test_conditional_reminder_is_not_forced_into_task(self):
        text = "如果下午五点下雨就提醒我带伞"
        model = ParsedCommand("clarify", message="这个条件提醒目前不支持")
        command = _reconcile_commands(text, model, parse_command(text), None)
        self.assertEqual(command.action, "clarify")
        self.assertEqual(command.message, model.message)

    def test_chat_flow_persists_today_task_for_wechat_user(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        try:
            with Session(engine) as db:
                reply = handle_chat_message(
                    "下午五点提醒我洗澡",
                    db,
                    conversation_id="wechat:test-user",
                    use_model=False,
                )
                task = db.scalar(select(Task))
                self.assertEqual(reply.action, "create")
                self.assertEqual(task.due_at, datetime(2026, 9, 30, 17, 0))
                self.assertEqual(task.owner_id, "wechat:test-user")
        finally:
            engine.dispose()


if __name__ == "__main__":
    import unittest

    unittest.main()
