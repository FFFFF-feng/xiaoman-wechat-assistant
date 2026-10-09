"""运行 python -m app.wechat_preflight：仅检查配置，不联网、不发送、不修改数据库。"""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse

from .adapters.wechat_official import wechat_channel_status
from .wechat_notifications import official_mode


def check() -> tuple[list[str], list[str]]:
    status = wechat_channel_status()
    errors = list(status["configuration_errors"]) + list(status["reminders"]["configuration_errors"])
    warnings = []
    if not status["enabled"]:
        errors.append("WECHAT_ENABLED 尚未启用")
    if not status["callback_token_configured"]:
        errors.append("WECHAT_TOKEN 未填写")
    if not status["outbound_configured"]:
        errors.append("WECHAT_APP_ID / WECHAT_APP_SECRET 未填写，无法主动回复或提醒")
    if status["reply_mode"] not in {"auto", "async", "passive"}:
        errors.append("WECHAT_REPLY_MODE 必须为 auto、async 或 passive")
    base = urlparse(os.getenv("PUBLIC_BASE_URL", "").strip())
    if base.scheme != "https" or not base.hostname or base.username or base.password or base.path not in {"", "/"} or base.query or base.fragment:
        errors.append("PUBLIC_BASE_URL 需要填写不带路径的 HTTPS 公网域名")
    try:
        if official_mode():
            if status["encryption"] != "safe":
                warnings.append("正式号建议 safe 模式；平台模式必须与本地相同")
            if status["reply_mode"] == "passive":
                warnings.append("被动回复有微信响应时限，使用大模型建议 async，前提是账号有客服消息权限")
            if not os.getenv("WECHAT_ORIGINAL_ID", "").strip():
                warnings.append("建议填写公众号原始 ID（gh_ 开头），不是 AppID")
    except RuntimeError:
        pass  # 状态检查已经列出了配置错误。
    warnings.append("离线检查不代表账号拥有发送权限，仍须核对平台接口权限并用正式号实测")
    return errors, warnings


def main() -> int:
    errors, warnings = check()
    print(json.dumps({"configuration_ok": not errors, "errors": errors, "warnings": warnings,
                      "network_requests": 0, "permission_verified": False}, ensure_ascii=False, indent=2))
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
