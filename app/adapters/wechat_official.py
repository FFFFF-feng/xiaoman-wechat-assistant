from __future__ import annotations

import logging
import hashlib
import os
import threading
import time

import httpx

from .base import ChatAdapter

logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
CUSTOM_MESSAGE_URL = "https://api.weixin.qq.com/cgi-bin/message/custom/send"
TEMPLATE_MESSAGE_URL = "https://api.weixin.qq.com/cgi-bin/message/template/send"
SUBSCRIBE_MESSAGE_URL = "https://api.weixin.qq.com/cgi-bin/message/subscribe/bizsend"


class WechatAPIError(RuntimeError):
    def __init__(self, code: object, message: str):
        self.code = str(code)
        super().__init__(f"微信接口错误 {self.code}：{message[:200]}")


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def wechat_channel_status() -> dict[str, object]:
    from ..wechat_crypto import WechatCrypto, encryption_mode
    from ..wechat_notifications import notification_config_status
    app_id = _env("WECHAT_APP_ID")
    app_secret = _env("WECHAT_APP_SECRET")
    token = _env("WECHAT_TOKEN")
    reply_mode = _env("WECHAT_REPLY_MODE").lower() or "auto"
    errors = []
    try:
        mode = encryption_mode()
        if mode != "plain":
            WechatCrypto.from_env()
    except ValueError as exc:
        mode = "invalid"
        errors.append(str(exc))
    return {
        "enabled": _env("WECHAT_ENABLED").lower() == "true",
        "callback_token_configured": bool(token),
        "outbound_configured": bool(app_id and app_secret),
        "reply_mode": reply_mode,
        "encryption": mode,
        "encryption_key_configured": bool(_env("WECHAT_ENCODING_AES_KEY")),
        "account_mode": _env("WECHAT_ACCOUNT_MODE") or "test",
        "configuration_errors": errors,
        "reminders": notification_config_status(),
        "callback_path": "/api/channels/wechat/callback",
    }


class WeChatOfficialAdapter(ChatAdapter):
    """微信公众号客服消息适配器，不使用个人微信 Hook。"""

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._token_lock = threading.Lock()
        self._credential_key: str | None = None

    @property
    def configured(self) -> bool:
        return bool(_env("WECHAT_APP_ID") and _env("WECHAT_APP_SECRET"))

    def _get_access_token(self, force_refresh: bool = False) -> str:
        if not self.configured:
            raise RuntimeError("未配置 WECHAT_APP_ID 或 WECHAT_APP_SECRET")
        now = time.time()
        credential_key = hashlib.sha256((_env("WECHAT_APP_ID") + "\0" + _env("WECHAT_APP_SECRET")).encode()).hexdigest()
        if not force_refresh and self._access_token and now < self._expires_at and self._credential_key == credential_key:
            return self._access_token

        with self._token_lock:
            now = time.time()
            if not force_refresh and self._access_token and now < self._expires_at and self._credential_key == credential_key:
                return self._access_token
            try:
                response = httpx.get(
                    TOKEN_URL,
                    params={"grant_type": "client_credential", "appid": _env("WECHAT_APP_ID"),
                            "secret": _env("WECHAT_APP_SECRET")}, timeout=15.0,
                )
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                # 不把带 AppSecret/access_token 的 URL 写入异常日志或数据库。
                raise RuntimeError(f"获取微信令牌的网络请求失败（{type(exc).__name__}）") from None
            access_token = data.get("access_token")
            if not access_token:
                raise WechatAPIError(data.get("errcode"), str(data.get("errmsg", "获取 access_token 失败")))
            expires_in = max(300, int(data.get("expires_in", 7200)))
            self._access_token = str(access_token)
            self._expires_at = time.time() + expires_in - 120
            self._credential_key = credential_key
            return self._access_token

    def _post_once(self, url: str, payload: dict, force_refresh: bool = False) -> dict:
        access_token = self._get_access_token(force_refresh=force_refresh)
        try:
            response = httpx.post(url, params={"access_token": access_token}, json=payload, timeout=15.0)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"发送微信消息的网络请求失败（{type(exc).__name__}）") from None

    def _post(self, url: str, payload: dict) -> dict:
        data = self._post_once(url, payload)
        if data.get("errcode") in {40014, 42001}:
            data = self._post_once(url, payload, force_refresh=True)
        if data.get("errcode") != 0:
            raise WechatAPIError(data.get("errcode"), str(data.get("errmsg", "消息发送失败")))
        return data

    def send(self, recipient: str, content: str) -> bool:
        payload = {"touser": recipient, "msgtype": "text", "text": {"content": content[:2000]}}
        if _env("WECHAT_AI_LABEL").lower() == "true":
            payload["aimsgcontext"] = {"is_ai_msg": 1}
        self._post(CUSTOM_MESSAGE_URL, payload)
        logger.info("微信公众号消息发送成功 recipient_suffix=%s", recipient[-6:])
        return True

    def send_notification(self, channel: str, recipient: str, template_id: str, data: dict) -> dict:
        if channel not in {"template", "subscribe"}:
            raise ValueError("未知通知通道")
        payload = {"touser": recipient, "template_id": template_id, "data": data}
        # 不包含用户私人日程令牌，不跳转公开首页。可从微信聊天另行打开日程。
        if channel == "subscribe":
            payload.update({"lang": "zh_CN", "miniprogram_state": "formal"})
        return self._post(TEMPLATE_MESSAGE_URL if channel == "template" else SUBSCRIBE_MESSAGE_URL, payload)


# 聊天回复和定时提醒共用一个 access_token 缓存，避免重复获取令牌。
wechat_adapter = WeChatOfficialAdapter()
