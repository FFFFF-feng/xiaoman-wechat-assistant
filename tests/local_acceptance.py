"""Explicit operator acceptance: isolated DB/bindings, real model, no Weixin calls.

Run through clawbot-local/scripts/Run-LocalAcceptance.mjs, not unittest discovery.
Credentials and schedule bearers are never included in results.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient`")
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from app import clawbot_delivery, model_parser, scheduler
from app.clawbot_identity import clawbot_owner
from app.database import SessionLocal, engine
from app.main import app
from app.models import ClawbotDelivery, ClawbotMessage, Reminder, ScheduleAccess, Task
from app.parser import parse_command

logging.getLogger("app.model_parser").disabled = True
logging.getLogger("app.scheduler").disabled = True
logging.getLogger("httpx").setLevel(logging.WARNING)
local_tz = ZoneInfo("Asia/Shanghai")
today = datetime.now(local_tz).replace(tzinfo=None).date()
bridge_key = os.environ["CLAWBOT_BRIDGE_TOKEN"]
headers = {"Authorization": f"Bearer {bridge_key}"}
client = TestClient(app)  # No lifespan: never start a competing background scheduler.
results = []
tasks = {}
serial = 0


def require(condition, reason):
    if not condition:
        raise AssertionError(reason)


def case(name, callback):
    started = time.perf_counter()
    try:
        detail = callback() or {}
        result = {"name": name, "status": "PASS", "seconds": round(time.perf_counter() - started, 3), **detail}
    except Exception as error:
        result = {"name": name, "status": "FAIL", "seconds": round(time.perf_counter() - started, 3),
                  "error_type": type(error).__name__}
        if isinstance(error, AssertionError):
            result["reason"] = str(error)  # Only locally defined assertions, never raw API response bodies.
    results.append(result)
    print("CASE " + json.dumps(result, ensure_ascii=False), flush=True)


def send(text, user="Alice", mid=None):
    global serial
    serial += 1
    response = client.post("/api/internal/clawbot/chat", headers=headers,
        json={"account_id": "acceptance-bot", "sender_id": user, "message_id": mid or f"acceptance-{serial}", "content": text})
    require(response.status_code == 200, "bridge request was not accepted")
    return response.json()


def count_tasks():
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(Task))


def task_date(task):
    return datetime.fromisoformat(task["due_at"])


def chat_case():
    before = count_tasks()
    first = send("你好，简单介绍一下自己")
    second = send("我今天有点累，能陪我聊两句吗？")
    third = send("我刚才说我今天感觉怎么样？")
    require(all(v["action"] == "chat" and v["parser"] == "model" for v in (first, second, third)), "real model chat did not succeed")
    require("小满" in first["reply"] and "累" in third["reply"], "persona/history response failed")
    require(count_tasks() == before, "ordinary chat created a task")
    return {"model_calls": 3, "actions": [v["action"] for v in (first, second, third)]}


def create_today():
    require(datetime.now(local_tz).hour < 23, "default-today fixture needs an earlier test time")
    result = send("提醒我晚上十一点五十分整理验收甲")
    require(result["action"] == "create" and result["task"], "implicit date did not create a task")
    require(task_date(result["task"]) == datetime.combine(today, datetime.min.time()).replace(hour=23, minute=50), "implicit date/time was wrong")
    tasks["today"] = result["task"]
    return {"parser": result["parser"], "due_at": result["task"]["due_at"]}


def create_tomorrow_and_dedup():
    result = send("明天上午十点提醒我整理验收乙", mid="dedup-fixture")
    require(result["action"] == "create" and result["task"], "tomorrow task was not created")
    require(task_date(result["task"]).date() == today + timedelta(days=1), "explicit tomorrow changed")
    before = count_tasks()
    repeated = send("明天上午十点提醒我整理验收乙", mid="dedup-fixture")
    require(repeated == result and count_tasks() == before, "duplicate message was executed twice")
    tasks["tomorrow"] = result["task"]
    return {"parser": result["parser"], "task_count_after_duplicate": before}


def clarify_followup():
    before = count_tasks()
    first = send("提醒我整理验收丙")
    require(first["action"] == "clarify" and count_tasks() == before, "missing time created a task")
    second = send("明天上午十一点")
    require(second["action"] == "create" and second["task"], "time-only follow-up was not merged")
    require(task_date(second["task"]) == datetime.combine(today + timedelta(days=1), datetime.min.time()).replace(hour=11), "follow-up date/time was wrong")
    tasks["followup"] = second["task"]
    return {"actions": [first["action"], second["action"]], "parser": second["parser"]}


def no_side_effect(text, expected="clarify"):
    before = count_tasks()
    result = send(text)
    require(result["action"] == expected, "unexpected intent routing")
    require(count_tasks() == before, "clarification/chat unexpectedly created a task")
    return {"action": result["action"], "parser": result["parser"]}


def bob_and_list():
    other = send("明天下午三点提醒我整理另一用户事项", user="Bob")
    require(other["action"] == "create" and other["task"], "second user's fixture was not created")
    tasks["bob"] = other["task"]
    alice = send("查询待办")
    bob = send("查询待办", user="Bob")
    require(alice["action"] == bob["action"] == "list_open", "pending list intent failed")
    require(other["task"]["title"] not in alice["reply"], "Alice can read Bob's tasks")
    require(tasks["today"]["title"] not in bob["reply"], "Bob can read Alice's tasks")
    return {"owners_isolated": True}


def chat_task_changes():
    result = send(f"把{tasks['tomorrow']['title']}改到后天上午十一点")
    require(result["action"] == "snooze" and result["task"]["id"] == tasks["tomorrow"]["id"], "reschedule matched the wrong task")
    require(task_date(result["task"]) == datetime.combine(today + timedelta(days=2), datetime.min.time()).replace(hour=11), "reschedule time failed")
    result = send(f"完成{tasks['today']['title']}")
    require(result["task"]["id"] == tasks["today"]["id"] and result["task"]["status"] == "done", "chat completion failed")
    result = send(f"取消{tasks['bob']['title']}", user="Bob")
    require(result["task"]["id"] == tasks["bob"]["id"] and result["task"]["status"] == "cancelled", "chat cancellation failed")
    return {"actions": ["snooze", "complete", "cancel"]}


def schedule_and_auth():
    result = send("打开日程")
    match = re.search(r"\[点击打开我的日程\]\(https://acceptance\.example\.com/schedule#access=([A-Za-z0-9_-]+)\)", result["reply"])
    require(match is not None, "schedule reply is not a Markdown link")
    token = match[1]
    auth = {"Authorization": f"Bearer {token}"}
    url = f"/api/schedule/tasks?start={today}&end={today + timedelta(days=7)}"
    response = client.get(url, headers=auth)
    require(response.status_code == 200, "authorized schedule could not be read")
    require(tasks["bob"]["id"] not in [r["id"] for r in response.json()], "schedule leaked another owner's task")
    require(client.get(url).status_code == 401, "schedule allowed anonymous access")
    require(client.post(f"/api/schedule/tasks/{tasks['bob']['id']}/cancel", headers=auth).status_code == 404, "cross-user write allowed")
    require(client.post(f"/api/schedule/tasks/{tasks['today']['id']}/complete", headers=auth).status_code == 409, "finished task allowed another completion")
    task_id = tasks["followup"]["id"]
    require(client.post(f"/api/schedule/tasks/{task_id}/snooze", headers=auth,
        json={"due_at": (datetime.now(local_tz) + timedelta(days=3)).isoformat()}).status_code == 200, "schedule reschedule failed")
    require(client.post(f"/api/schedule/tasks/{task_id}/cancel", headers=auth).status_code == 200, "schedule cancellation failed")
    with SessionLocal() as db:
        record = db.scalar(select(ClawbotMessage).where(ClawbotMessage.content == "打开日程"))
        require(token not in record.reply_content and token not in record.response_json, "schedule bearer was saved in history")
        access = db.scalar(select(ScheduleAccess).where(ScheduleAccess.token_hash == hashlib.sha256(token.encode()).hexdigest()))
        access.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    require(client.get(url, headers=auth).status_code == 401, "expired authorization was accepted")
    return {"anonymous": 401, "cross_owner": 404, "expired": 401, "bearer_not_saved": True}


def boundary_requests():
    base = {"account_id": "acceptance-bot", "sender_id": "Alice", "message_id": "boundary", "content": "查询待办"}
    require(client.post("/api/internal/clawbot/chat", json=base).status_code == 401, "bridge accepted anonymous call")
    require(client.post("/api/internal/clawbot/chat", headers=headers, json={**base, "sender_id": "Mallory"}).status_code == 403, "unbound sender accepted")
    require(client.post("/api/internal/clawbot/chat", headers=headers, json={**base, "owner_id": "other"}).status_code == 422, "client-selected owner accepted")
    require(client.post("/api/internal/clawbot/chat", headers=headers, json={**base, "content": " "}).status_code == 422, "blank input accepted")
    require(client.post("/api/internal/clawbot/chat", headers=headers, json={**base, "content": "x" * 9000}).status_code == 422, "oversized content accepted")
    require(client.post("/api/internal/clawbot/chat", headers=headers, json={**base, "message_id": "dedup-fixture", "content": "different"}).status_code == 409, "conflicting duplicate accepted")
    return {"statuses": [401, 403, 422, 422, 422, 409]}


def queue_state():
    owner = clawbot_owner("acceptance-bot", "Alice")
    now = datetime.now(local_tz).replace(tzinfo=None)
    with SessionLocal() as db:
        task = Task(title="隔离提醒状态验收", owner_id=owner, due_at=now - timedelta(seconds=2), status="open", reminder_channel="clawbot")
        db.add(task); db.commit(); task_id = task.id
    scheduler.check_due_tasks(); scheduler.check_due_tasks()
    with SessionLocal() as db:
        reminder = db.scalar(select(Reminder).where(Reminder.task_id == task_id))
        require(reminder is not None and reminder.status == "queued", "scheduler did not enqueue")
        require(db.scalar(select(func.count()).select_from(ClawbotDelivery).where(ClawbotDelivery.reminder_id == reminder.id)) == 1, "scheduler queued a duplicate")
    item = client.post("/api/internal/clawbot/outbox/claim", headers=headers).json()["delivery"]
    require(item is not None, "queued reminder could not be claimed")
    lease = {"claim_token": item["claim_token"]}
    require(client.post(f"/api/internal/clawbot/outbox/{item['id']}/start", headers=headers, json=lease).status_code == 200, "send start failed")
    ack = {**lease, "outcome": "accepted", "message_id": "isolated-fake-receipt"}
    require(client.post(f"/api/internal/clawbot/outbox/{item['id']}/result", headers=headers, json=ack).status_code == 200, "acceptance ACK failed")
    require(client.post(f"/api/internal/clawbot/outbox/{item['id']}/result", headers=headers, json=ack).status_code == 200, "repeat ACK was not idempotent")
    require(client.post("/api/internal/clawbot/outbox/claim", headers=headers).json()["delivery"] is None, "accepted delivery was queued again")
    return {"state_machine_passed": True, "real_weixin_send": False}


def persisted():
    original_count = count_tasks()
    reopened = create_engine(str(engine.url), connect_args={"check_same_thread": False})
    try:
        with Session(reopened) as db:
            require(db.scalar(select(func.count()).select_from(Task)) == original_count, "tasks were not durable across new connections")
            require(db.get(Task, tasks["today"]["id"]).status == "done", "completion was not persisted")
    finally:
        reopened.dispose()
    return {"new_connection_verified": True}


def degraded_boundary(text):
    before = count_tasks()
    with patch("app.chat_service.parse_with_model", return_value=None):
        result = send(text)
    require(result["action"] not in {"create", "snooze", "complete", "cancel"},
            f"model outage incorrectly executed action={result['action']} for a negative/conditional request")
    require(count_tasks() == before, "model outage caused an unintended task write")
    return {"action": result["action"], "parser": result["parser"]}


def degraded_explicit_request():
    with patch("app.chat_service.parse_with_model", return_value=None):
        result = send("明天上午十点提醒我整理降级明确事项")
    require(result["action"] == "create", "explicit request was not handled by local rules")
    return {"action": result["action"], "parser": result["parser"]}


def safe_model_boundary(text):
    before = count_tasks()
    result = send(text)
    require(result["action"] in {"chat", "clarify", "unknown"}, "real model boundary executed a task mutation")
    require(count_tasks() == before, "real model boundary created a task")
    return {"action": result["action"], "parser": result["parser"]}


def affirmative_negative_content():
    before = count_tasks()
    result = send("明天上午十点提醒我不要忘记带验收钥匙")
    require(result["action"] == "create" and result["task"], "affirmative reminder was mistakenly blocked")
    require(task_date(result["task"]) == datetime.combine(today + timedelta(days=1), datetime.min.time()).replace(hour=10), "affirmative reminder time was wrong")
    require(count_tasks() == before + 1, "affirmative reminder was not persisted once")
    return {"action": result["action"], "parser": result["parser"]}


boundaries_only = "--boundaries-only" in sys.argv
if boundaries_only:
    case("模型不可用：明确请求仍能按规则创建", degraded_explicit_request)
    case("模型不可用：否定提醒不能创建任务", lambda: degraded_boundary("不要提醒我明天上午十点整理否定事项"))
    case("模型不可用：条件提醒不能创建任务", lambda: degraded_boundary("如果明天下午三点下雨就提醒我整理条件降级事项"))
else:
    case("真实模型聊天、人设、多轮历史及无任务副作用", chat_case)
    case("不写日期默认当天", create_today)
    case("明天任务与同消息去重", create_tomorrow_and_dedup)
    case("缺少时间追问及下一轮补全", clarify_followup)
    case("过期当天时刻不能擅自改成明天", lambda: no_side_effect("提醒我早上八点整理过去事项"))
    case("非法小时拒绝创建", lambda: no_side_effect("明天25点提醒我整理错误时间"))
    case("条件提醒不冒充定时提醒", lambda: no_side_effect("如果明天下雨就提醒我整理条件事项"))
    case("真实模型：否定提醒不执行任务操作", lambda: safe_model_boundary("不要提醒我明天上午十点整理否定事项"))
    case("真实模型：带时间的条件提醒不执行任务操作", lambda: safe_model_boundary("如果明天下午三点下雨就提醒我整理条件降级事项"))
    case("真实模型：提醒内容有不要忘记仍可创建", affirmative_negative_content)
    case("双用户待办与历史隔离", bob_and_list)
    case("聊天改期、完成与取消", chat_task_changes)
    case("日程操作、越权拒绝、令牌脱敏与过期", schedule_and_auth)
    case("鉴权、绑定、字段、长度与冲突边界", boundary_requests)
    case("调度入队、状态机与重复 ACK（无真实微信发送）", queue_state)
    case("新数据库连接可读取持久任务状态", persisted)

client.close()
report = {"time": datetime.now(local_tz).isoformat(timespec="seconds"), "scope": "isolated_degraded_model_safety" if boundaries_only else "isolated_database_real_model_no_weixin_send",
          "model": model_parser.MODEL_NAME, "model_available": model_parser.MODEL_AVAILABLE,
          "model_last_error": model_parser.LAST_MODEL_ERROR, "cases": results,
          "passed": sum(r["status"] == "PASS" for r in results), "failed": sum(r["status"] == "FAIL" for r in results)}
print("ACCEPTANCE_REPORT " + json.dumps(report, ensure_ascii=False), flush=True)
sys.exit(1 if report["failed"] else 0)
