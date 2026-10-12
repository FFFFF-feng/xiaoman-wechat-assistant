"""ClawBot 身份与公众号 OpenID 分离，不能由消息正文指定任务归属。"""
import hashlib
import re


def clawbot_owner(account_id: str, sender_id: str) -> str:
    identity = f"{account_id}\0{sender_id}".encode("utf-8")
    return "clawbot:" + hashlib.sha256(identity).hexdigest()


def is_clawbot_owner(owner_id: str | None) -> bool:
    return bool(owner_id and re.fullmatch(r"clawbot:[0-9a-f]{64}", owner_id))
