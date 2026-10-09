#!/usr/bin/env bash
# 在 screen 内运行。仅启动本项目，不操作 systemd、不修改 Nginx、不杀其他进程。
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
task_env=(env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy)
"${task_env[@]}" .venv/bin/python -m app.wechat_preflight
exec "${task_env[@]}" .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 18080 --workers 1
