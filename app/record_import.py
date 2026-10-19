"""Persistent, explicit text collection; all collected messages are read-only data."""
from datetime import datetime, timedelta
import os

from . import materials
from .assistant_preferences import snapshot
from .models import ClawbotImportSession
from .schemas import ChatResponse

START = {"整理聊天记录", "开始整理聊天记录", "合并聊天记录", "开始合并聊天记录"}
FINISH = {"整理完毕", "结束整理", "完成整理"}
CANCEL = {"取消整理", "退出整理"}


def collect(db, owner, content):
    lines = content.strip().splitlines()
    head = lines[0].strip() if lines else ""
    session = db.get(ClawbotImportSession, owner)
    now = datetime.now()
    if os.getenv("CLAWBOT_MATERIALS_ENABLED", "true").lower() != "true":
        if session is not None or head in START:
            if session is not None:
                db.delete(session)
            return ChatResponse(action="import_collect", reply="资料整理已停用，本条未作为任务执行。请先发普通文字。")
        return None
    if head in START:
        if session is None:
            session = ClawbotImportSession(owner_id=owner, content="", count=0, expires_at=now + timedelta(minutes=15))
            db.add(session)
            db.flush()
        else:
            # Re-entering must not silently destroy an already collected record.
            if session.expires_at < now:
                session.content, session.count = "", 0
            session.expires_at = now + timedelta(minutes=15)
        lines = lines[1:]
    elif session is None:
        return None
    elif session.expires_at < now:
        db.delete(session)
        return ChatResponse(action="import_collect", reply="聊天记录整理已超时，本条没有作为任务执行。请发“整理聊天记录”重新开始。")
    if head in CANCEL:
        db.delete(session)
        return ChatResponse(action="import_collect", reply="已退出聊天记录整理，未创建提醒。")
    finish = bool(lines and lines[-1].strip() in FINISH)
    if finish:
        lines = lines[:-1]
    additional = "\n".join(lines).strip()
    if additional:
        combined = (session.content + "\n" + additional).strip()
        if len(combined) > materials.MAX_TEXT or session.count + len(lines) > materials.MAX_RECORDS:
            return ChatResponse(action="import_collect", reply="已达到200条或24000字上限，本条未加入。请发“整理完毕”处理已收内容，或“取消整理”退出。")
        session.content, session.count = combined, session.count + len(lines)
        # A fixed 15-minute window avoids indefinite accidental capture.
    if finish:
        if not session.content:
            return ChatResponse(action="import_collect", reply="尚未收到聊天正文，请复制后粘贴文字，再发“整理完毕”。微信转发列表目前无法选择此ClawBot。")
        try:
            reply = materials.summarize(text=materials.record_text(session.content), preferences=snapshot(db, owner))
        except materials.MaterialError as error:
            return ChatResponse(action="import_collect", reply=str(error) + "。内容已保留，可发新的“整理完毕”重试，或“取消整理”退出。")
        db.delete(session)
        return ChatResponse(action="material_summary", parser="model", reply=reply + materials.FOOTER)
    return ChatResponse(action="import_collect", reply=("已进入聊天记录整理，15分钟内可分多条粘贴文字。微信转发列表目前无法选择此ClawBot。"
        "发“整理完毕”统一生成摘要和待办草稿，发“取消整理”退出。此期间文字仅作为资料，不执行任务。"
        if head in START else f"已加入聊天记录，累计{session.count}行。发“整理完毕”统一整理；未创建提醒。"))
