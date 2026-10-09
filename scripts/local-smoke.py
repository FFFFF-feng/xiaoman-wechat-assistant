"""对本机正在运行的服务做自检；只创建带自检标识的本地任务，随后结束它们。"""
import argparse
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise ValueError("端口无效")
    task_ids = []
    checks = {}
    with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", trust_env=False, timeout=20) as client:
        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        health = request("GET", "/health")
        assert health["status"] == "ok" and health["scheduler_running"]
        assert not health["channels"]["wechat_official"]["enabled"], "这是本地模拟自检，请不要对已启用的正式号实例执行"
        assert health["model"]["mode"] == "rules_only", "本地自检要求 rules_only，避免消耗真实模型额度"
        checks["health"] = health["version"]
        for path in ("/", "/schedule", "/static/app.js", "/static/schedule.js", "/static/schedule.css"):
            assert client.get(path).status_code == 200, path
        checks["pages_and_assets"] = "passed"
        assert client.get("/api/schedule/tasks?start=2026-10-04&end=2026-10-04").status_code == 401
        checks["private_schedule_auth"] = "401 without token"

        try:
            response = request("POST", "/api/chat", json={"content": "两分钟后提醒我本地自检任务"})
            assert response["action"] == "create", response
            task_id = response["task"]["id"]
            task_ids.append(task_id)
            checks["chat_create"] = "passed"
            due = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(minutes=10)).isoformat()
            result = request("POST", f"/api/tasks/{task_id}/snooze", json={"due_at": due})
            assert result["status"] == "snoozed"
            result = request("POST", f"/api/tasks/{task_id}/complete")
            assert result["status"] == "done"
            checks["snooze_and_complete"] = "passed"

            due = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(seconds=3)).isoformat()
            result = request("POST", "/api/tasks", json={"title": "本地自检：定时提醒", "due_at": due})
            task_id = result["id"]
            task_ids.append(task_id)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                notifications = request("GET", "/api/notifications?limit=100")
                reminder = next((item for item in notifications if item["task_id"] == task_id), None)
                if reminder:
                    assert reminder["status"] == "sent", reminder
                    assert reminder["channel"] == "local", reminder
                    checks["live_scheduler_reminder"] = "sent via local mock"
                    break
                time.sleep(0.5)
            else:
                raise AssertionError("15秒内没有观察到提醒")
            result = request("POST", f"/api/tasks/{task_id}/cancel")
            assert result["status"] == "cancelled"
            checks["cancel"] = "passed"
        finally:
            # 不删除用户数据；自检任务只结束/取消，不留下未来未完成提醒。
            for task_id in task_ids:
                tasks = request("GET", "/api/tasks")
                task = next((item for item in tasks if item["id"] == task_id), None)
                if task and task["status"] not in {"done", "cancelled"}:
                    request("POST", f"/api/tasks/{task_id}/cancel")

    print(json.dumps({"ok": True, "checks": checks, "real_wechat_messages_sent": 0,
                      "model_calls": 0, "test_tasks_closed": True}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
