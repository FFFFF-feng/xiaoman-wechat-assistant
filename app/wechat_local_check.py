"""本机正式号联调检查：公网路径封闭性、真实签名握手、可选获取微信令牌；不发送消息。"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import secrets
import time

import httpx

from .adapters.wechat_official import WechatAPIError, wechat_adapter
from .wechat_crypto import WechatCrypto
from .wechat_preflight import check


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-token", action="store_true", help="向微信请求一次 access_token，不打印令牌，不发送消息")
    args = parser.parse_args()
    # 只清理此检查进程的代理，避免继承桌面 SOCKS 设置；不改系统代理。
    for name in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        os.environ.pop(name, None)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    errors, warnings = check()
    result = {"configuration_ok": not errors, "errors": errors, "warnings": warnings,
              "real_messages_sent": 0, "send_permission_verified": False}
    if errors:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2

    base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    callback = base + "/api/channels/wechat/callback"
    timestamp, nonce = str(int(time.time())), secrets.token_hex(8)
    echo = "xiaoman-local-verification-" + secrets.token_hex(8)
    token = os.getenv("WECHAT_TOKEN", "").strip()
    signature = hashlib.sha1("".join(sorted((token, timestamp, nonce))).encode()).hexdigest()
    crypto = WechatCrypto.from_env()
    encrypted_echo = crypto.encrypt(echo.encode())
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=20) as client:
            response = client.get(callback, params={"signature": signature, "timestamp": timestamp,
                                                   "nonce": nonce, "echostr": echo})
            result["standard_handshake"] = response.status_code == 200 and response.text == echo
            response = client.get(callback, params={"msg_signature": crypto.signature(encrypted_echo, timestamp, nonce),
                                                   "timestamp": timestamp, "nonce": nonce,
                                                   "echostr": encrypted_echo, "encrypt_type": "aes"})
            result["encrypted_handshake"] = response.status_code == 200 and response.text == echo
            blocked = {path: client.get(base + path).status_code for path in
                       ("/", "/health", "/docs", "/api/chat", "/api/tasks", "/.env")}
            result["public_debug_paths_closed"] = all(code == 404 for code in blocked.values())
            result["public_debug_statuses"] = blocked
            response = client.get(base + "/api/schedule/tasks", params={"start": "2026-10-04", "end": "2026-10-04"})
            result["schedule_requires_token"] = response.status_code == 401
    except httpx.HTTPError as exc:
        result["network_error"] = type(exc).__name__
    if args.check_token:
        try:
            wechat_adapter._get_access_token()
            result["wechat_access_token_obtained"] = True
        except WechatAPIError as exc:
            reasons = {"40164": "当前公网出口IP不在公众号白名单中，保留旧条目并追加当前IP",
                       "40125": "AppSecret不合法，请在本地重新核对",
                       "40001": "凭据不合法，请核对正式号AppID与AppSecret是否配对",
                       "40013": "AppID不合法", "40243": "AppSecret已被冻结",
                       "50004": "微信拒绝该账号调用token接口"}
            result["wechat_access_token_obtained"] = False
            result["wechat_error_code"] = exc.code
            result["wechat_error"] = reasons.get(exc.code, "微信接口拒绝请求，请根据错误码核对平台权限")
        except RuntimeError:
            result["wechat_access_token_obtained"] = False
            result["wechat_error"] = "访问微信接口失败；检查网络连接，没有打印任何密钥或请求URL"
    success = all(result.get(name, False) for name in
                  ("standard_handshake", "encrypted_handshake", "public_debug_paths_closed", "schedule_requires_token"))
    if args.check_token:
        success = success and result.get("wechat_access_token_obtained", False)
    result["ok"] = success
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
