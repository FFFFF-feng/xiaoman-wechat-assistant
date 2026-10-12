from datetime import datetime
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.chat_service import _reconcile_commands, handle_chat_message
from app.database import Base
from app.models import Task
from app.parser import ParsedCommand, parse_command, parse_time_followup


NOW = datetime(2026, 10, 12, 18, 0)
NEGATIVE = "不要提醒我明天上午十点整理否定事项"
CONDITIONAL = "如果明天下午三点下雨就提醒我整理条件事项"


class ReminderIntentSafetyTests(TestCase):
    def setUp(self):
        clock = patch("app.parser.now_local", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)

    def test_negative_reminder_variants_are_clarified(self):
        for prefix in ("不要", "不用", "不需要", "别", "请不要再", "我不希望你", "不要在明天十点"):
            with self.subTest(prefix=prefix):
                command = parse_command(prefix + "提醒我明天十点完成周报")
                self.assertEqual(command.action, "clarify")
                self.assertIsNone(command.due_at)

    def test_conditional_reminder_variants_are_clarified(self):
        for text in (CONDITIONAL, "假如明天十点有雨就提醒我带伞",
                     "要是两分钟后还没完成就提醒我", "一旦明天十点下雨就提醒我带伞",
                     "记得如果明天十点下雨就提醒我带伞"):
            with self.subTest(text=text):
                self.assertEqual(parse_command(text).action, "clarify")

    def test_negative_words_in_reminder_content_are_preserved(self):
        for title in ("不要忘记带钥匙", "不要取消预约", "不要完成错误步骤"):
            with self.subTest(title=title):
                command = parse_command("明天十点提醒我" + title)
                self.assertEqual(command.action, "create")
                self.assertEqual(command.title, title)
                self.assertEqual(command.due_at, datetime(2026, 10, 13, 10))

    def test_do_not_forget_to_remind_is_an_affirmative_request(self):
        self.assertEqual(parse_command("不要忘记明天十点提醒我带钥匙").action, "create")

    def test_timed_weather_check_is_not_a_conditional_trigger(self):
        for text in ("明天十点提醒我检查天气", "明天十点提醒我想想如果下雨怎么办"):
            with self.subTest(text=text):
                self.assertEqual(parse_command(text).action, "create")

    def test_explicit_existing_task_actions_still_take_precedence(self):
        for text, action in (("取消明天十点提醒我带钥匙", "cancel"),
                             ("完成预约提醒", "complete"),
                             ("把喝水提醒延后到明天十点", "snooze"),
                             ("查询明天提醒", "list_today")):
            with self.subTest(text=text):
                self.assertEqual(parse_command(text).action, action)

    def test_model_outage_does_not_write_tasks(self):
        with Session(self.engine) as db:
            task = Task(title="整理否定事项", due_at=datetime(2026, 10, 13, 10), status="open")
            db.add(task)
            db.commit()
            original = (task.id, task.title, task.due_at, task.status)
            with patch("app.chat_service.parse_with_model", return_value=None):
                for text in (NEGATIVE, CONDITIONAL):
                    with self.subTest(text=text):
                        response = handle_chat_message(text, db)
                        self.assertEqual(response.action, "clarify")
                        self.assertIsNone(response.task)
                        self.assertEqual(response.parser, "rules")
            rows = db.scalars(select(Task)).all()
            self.assertEqual([(r.id, r.title, r.due_at, r.status) for r in rows], [original])

    def test_incorrect_model_mutations_cannot_bypass_guard(self):
        for text in (NEGATIVE, CONDITIONAL):
            for action in ("create", "snooze", "complete", "cancel"):
                with self.subTest(text=text, action=action):
                    model = ParsedCommand(action, title="整理否定事项", due_at=datetime(2026, 10, 13, 10))
                    with Session(self.engine) as db, patch("app.chat_service.parse_with_model", return_value=model):
                        response = handle_chat_message(text, db)
                        self.assertEqual(response.action, "clarify")
                        self.assertEqual(response.parser, "hybrid")
                        self.assertIsNone(db.scalar(select(Task)))

    def test_existing_model_clarification_is_preserved(self):
        model = ParsedCommand("clarify", message="这个条件提醒目前不支持", source="model")
        result = _reconcile_commands(CONDITIONAL, model, parse_command(CONDITIONAL), None)
        self.assertIs(result, model)

    def test_time_followup_cannot_reactivate_rejected_request(self):
        for previous in ("不要提醒我带钥匙", "如果下雨就提醒我带伞"):
            with self.subTest(previous=previous):
                result = parse_time_followup("明天十点", previous)
                self.assertEqual(result.action, "clarify")
                self.assertIsNone(result.due_at)

    def test_model_outage_followup_stays_side_effect_free(self):
        with Session(self.engine) as db, patch("app.chat_service.parse_with_model", return_value=None):
            for previous in ("不要提醒我带钥匙", "如果下雨就提醒我带伞"):
                with self.subTest(previous=previous):
                    self.assertEqual(handle_chat_message(previous, db).action, "clarify")
                    self.assertEqual(handle_chat_message("明天十点", db).action, "clarify")
                    self.assertIsNone(db.scalar(select(Task)))

    def test_incorrect_model_create_cannot_override_followup_guard(self):
        with Session(self.engine) as db:
            handle_chat_message("如果下雨就提醒我带伞", db, use_model=False)
            model = ParsedCommand("create", title="带伞", due_at=datetime(2026, 10, 13, 10))
            with patch("app.chat_service.parse_with_model", return_value=model):
                self.assertEqual(handle_chat_message("明天十点", db).action, "clarify")
            self.assertIsNone(db.scalar(select(Task)))

    def test_model_outage_still_allows_affirmative_reminder(self):
        with Session(self.engine) as db, patch("app.chat_service.parse_with_model", return_value=None):
            result = handle_chat_message("明天十点提醒我不要忘记带钥匙", db)
            self.assertEqual(result.action, "create")
            self.assertEqual(result.task.title, "不要忘记带钥匙")
            self.assertEqual(result.task.due_at, datetime(2026, 10, 13, 10))
            self.assertEqual(len(db.scalars(select(Task)).all()), 1)
