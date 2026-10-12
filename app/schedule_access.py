from __future__ import annotations

import hashlib
import os
import re
import secrets
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .database import get_db
from .models import ScheduleAccess
from .clawbot_identity import is_clawbot_owner

LINK_LIFETIME = timedelta(minutes=30)
QUICK_TUNNEL_HOST = re.compile(r"^(?!api\.)[a-z0-9-]+\.trycloudflare\.com$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def public_base_url(request: Request) -> str | None:
    """优先使用显式配置；临时联调仅接受 Cloudflare Quick Tunnel 域名。"""
    configured = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not configured and os.getenv("CLAWBOT_ENABLED", "false").lower() == "true" and os.getenv("CLAWBOT_PUBLIC_URL_FILE"):
        # 本机隧道地址动态更新，无须重启 Python；监督进程每 10 秒续期。
        try:
            state = json.loads(Path(os.environ["CLAWBOT_PUBLIC_URL_FILE"]).read_text(encoding="utf-8"))
            seen = datetime.fromisoformat(state["updated_at"]).astimezone(timezone.utc)
            parsed = urlparse(state["url"])
            if (state.get("ready") is True and 0 <= (datetime.now(timezone.utc) - seen).total_seconds() < 60
                and parsed.scheme == "https" and QUICK_TUNNEL_HOST.fullmatch(parsed.netloc)
                and not (parsed.path or parsed.query or parsed.fragment)):
                configured = state["url"]
            else:
                return None
        except (OSError, ValueError, KeyError, TypeError):
            return None
    if configured:
        parsed = urlparse(configured)
        if (
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not (parsed.path or parsed.query or parsed.fragment)
        ):
            return configured
        return None

    for value in (request.headers.get("x-forwarded-host", ""), request.headers.get("host", "")):
        host = value.split(",", 1)[0].strip().lower()
        if QUICK_TUNNEL_HOST.fullmatch(host):
            return f"https://{host}"
    return None


def issue_schedule_link(db: Session, owner_id: str, base_url: str) -> str:
    if not ((owner_id.startswith("wechat:") and owner_id.removeprefix("wechat:")) or is_clawbot_owner(owner_id)):
        raise ValueError("仅能为已绑定的微信用户签发日程链接")
    token = secrets.token_urlsafe(32)
    now = utc_now()
    db.execute(delete(ScheduleAccess).where(ScheduleAccess.expires_at <= now))
    db.add(
        ScheduleAccess(
            token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            owner_id=owner_id,
            created_at=now,
            expires_at=now + LINK_LIFETIME,
        )
    )
    db.commit()
    # 放在 fragment 中：网页地址请求和服务器日志不会带上令牌。
    return f"{base_url.rstrip('/')}/schedule#access={token}"


def schedule_owner(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> str:
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="请从微信重新打开日程链接")
    token = authorization[len(prefix):].strip()
    if len(token) < 32 or len(token) > 100:
        raise HTTPException(status_code=401, detail="日程链接无效")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    access = db.scalar(select(ScheduleAccess).where(ScheduleAccess.token_hash == token_hash))
    if not access or access.expires_at <= utc_now():
        raise HTTPException(status_code=401, detail="日程链接已过期，请在微信发送“打开日程”获取新链接")
    from .wechat_notifications import official_mode, owner_for, recipient_for
    if access.owner_id.startswith("wechat:") and official_mode() and access.owner_id != owner_for(recipient_for(access.owner_id)):
        raise HTTPException(status_code=401, detail="公众号已经切换，请在当前正式号重新打开日程")
    return access.owner_id
