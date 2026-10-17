"""Default-on image outbox with persistent user opt-out. Timestamps are naive UTC."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .clawbot_identity import clawbot_owner
from .models import ClawbotStickerDelivery as Delivery, ClawbotStickerPreference as Preference
from .schedule_access import utc_now
from .schemas import ChatResponse

ASSETS = Path(__file__).resolve().parents[1] / "assets/stickers/phoenix-d"
CAPTIONS = {"morning":"早呀", "received":"收到", "cheer":"加油", "rest":"歇一下", "thanks":"谢谢你", "goodnight":"晚安",
            "laugh":"哈哈哈", "hug":"抱抱", "here":"在呢", "sad":"委屈", "okay":"好哒", "comfort":"辛苦啦", "wait":"等等我", "go":"出发"}
LOCAL = ZoneInfo("Asia/Shanghai")


def enabled():
    return (os.getenv("CLAWBOT_ENABLED", "false").lower() == "true"
            and os.getenv("CLAWBOT_STICKERS_ENABLED", "true").lower() == "true")


def local(now):
    return now.replace(tzinfo=timezone.utc).astimezone(LOCAL)


def normalized(text):
    return re.sub(r"[\s，,。.!！?？]", "", text)


def available(sticker):
    if sticker not in CAPTIONS:
        return False
    try:
        catalog = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
        entry = catalog[sticker]
        if entry["file"] != sticker + ".png":
            return False
        root = ASSETS.resolve(); path = (root / entry["file"]).resolve()
        if path.parent != root or (root / entry["file"]).is_symlink():
            return False
        data = path.read_bytes()
        return (len(data) <= 4*1024*1024 and data.startswith(b"\x89PNG\r\n\x1a\n")
                and hashlib.sha256(data).hexdigest() == entry["sha256"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def preference(db, owner, now):
    """Initialize only missing preferences; never overwrite a user's opt-out."""
    pref = db.get(Preference, owner)
    if not pref:
        auto = enabled()
        try:
            with db.begin_nested():
                pref = Preference(owner_id=owner, daily_enabled=auto, chat_enabled=auto,
                                  daily_started_at=now if auto else None, last_active_at=now)
                db.add(pref)
                db.flush()
        except IntegrityError:
            # Scheduler and a new inbound message can initialize the same owner.
            pref = db.get(Preference, owner)
            if pref is None:
                raise
    return pref


def touch(db, owner):
    if not enabled():
        return
    pref = preference(db, owner, utc_now())
    pref.last_active_at = utc_now()
    db.commit()


def enqueue(db, owner, sticker, event, source, now, expires=None):
    if not available(sticker):
        return False
    if db.scalar(select(Delivery).where(Delivery.owner_id == owner, Delivery.event_key == event)):
        return True
    db.add(Delivery(owner_id=owner, sticker_id=sticker, event_key=event, source=source,
                    created_at=now, expires_at=expires or now + timedelta(minutes=5)))
    db.commit()
    return True


def command(db, owner, text, message_key):
    value = normalized(text)
    names = '|'.join(map(re.escape, sorted([*CAPTIONS.values(), '早安', '谢谢'], key=len, reverse=True)))
    request = re.fullmatch(r"(?:给我(?:一个)?|发个|来个|发送|发一张)(" + names + r")(?:表情|表情包)", value)
    manual_id = None
    if request:
        caption = {"早安":"早呀", "谢谢":"谢谢你"}.get(request[1], request[1])
        manual_id = next(k for k,v in CAPTIONS.items() if v == caption)
    elif value in {"给我一个表情包", "发个表情包", "来个表情包", "发表情包"}:
        manual_id = "received"
    elif re.fullmatch(r'(?:怎么|咋|为什么)(?:还)?(?:不|没|没有)(?:给我)?发(?:发)?表情(?:包)?(?:呢|呀|啊)?', value):
        manual_id = "hug"
    switches = {"开启日常表情":("daily_enabled",True), "关闭日常表情":("daily_enabled",False),
                "开启聊天表情":("chat_enabled",True), "关闭聊天表情":("chat_enabled",False)}
    stop_all = (value in {"关闭表情包", "不要再发表情", "不要再发表情包", "别发表情了", "别再发表情包", "关闭表情"}
                or re.fullmatch(r'(?:请|麻烦你|你能|能不能|可以)?(?:以后)?(?:不要|别)(?:再)?(?:给我)?发(?:送)?表情(?:包)?(?:了)?(?:吗)?', value) is not None)
    start_all = value in {"开启表情包", "恢复表情包", "可以发表情了"}
    if value not in switches and value not in {"表情包", "表情设置", "小满表情包"} and not manual_id and not stop_all and not start_all:
        return None
    def reply(content):
        return ChatResponse(action="chat", reply=content)
    stopping = stop_all or value in switches and not switches[value][1]
    if not enabled() and not stopping:
        return reply("表情包功能尚未开放，目前不会发送图片。")
    pref = preference(db, owner, utc_now())
    if stop_all:
        pref.chat_enabled = pref.daily_enabled = False
        db.execute(update(Delivery).where(Delivery.owner_id == owner,
            Delivery.status.in_(["pending", "claimed"])).values(status="cancelled", claim_hash=None))
        db.commit()
        return reply("聊天配图和日常问候都已关闭，尚未开始的表情也已取消。想恢复时说“开启表情包”；临时想看一张也可以直接找我要。")
    if start_all:
        if not all(available(s) for s in CAPTIONS):
            return reply("表情素材还没准备齐，暂未开启。")
        if not pref.daily_enabled:
            pref.daily_started_at = utc_now()
        pref.chat_enabled = pref.daily_enabled = True
        db.commit()
        return reply("聊天配图和日常问候都已开启。聊天配图没有次数和冷却限制；早安按北京时间每天9点尝试发送，开启当天不补发。随时说“关闭表情包”。")
    if value in switches:
        field, on = switches[value]
        if on and not all(available(s) for s in CAPTIONS):
            return reply("表情素材还没准备齐，暂未开启。")
        previous = getattr(pref, field)
        setattr(pref, field, on)
        if field == "daily_enabled" and on and not previous:
            pref.daily_started_at = utc_now()
        if not on:
            source = "daily" if field == "daily_enabled" else "chat"
            db.execute(update(Delivery).where(Delivery.owner_id == owner, Delivery.source == source,
                Delivery.status.in_(["pending", "claimed"])).values(status="cancelled", claim_hash=None))
        db.commit()
        if field == "daily_enabled":
            if on and previous:
                return reply("日常表情已经开启，仍按北京时间每天9点尝试问候；不会因为重复开启多发。")
            return reply("已开启日常表情：从明天起，每天北京时间9点尝试发一张早安表情，错过不补发。随时说“关闭日常表情”。" if on
                         else "日常表情已关闭，后续不再自动问早安。想看时仍可直接索取。")
        return reply("聊天表情已开启。打招呼、分享心情和普通闲聊都会搭配合适的表情，不设次数和冷却限制。随时说“关闭聊天表情”。" if on
                     else "聊天表情已关闭，正常文字回复不受影响。")
    if manual_id:
        now = utc_now()
        if not enqueue(db, owner, manual_id, "manual:"+message_key, "manual", now):
            return reply("这张表情暂时不可用，没有安排发送。")
        return reply("“"+CAPTIONS[manual_id]+"”表情已加入发送队列。若图片没到，请稍后再看，平台受理不代表已经送达。")
    return reply("想看哪张，直接说“给我抱抱表情包”。现在有："+'、'.join(CAPTIONS.values())+"。\n聊天配图："+
                 ("开" if pref.chat_enabled else "关")+"；每日9点问候："+("开" if pref.daily_enabled else "关")+
                 "。默认自动开启，不设次数和冷却限制。说“关闭表情包”暂停，说“开启表情包”恢复，也可分别开关聊天和日常表情。")


def choose_sticker(text, message_key, previous=None):
    value = normalized(text).lower()
    if (len(value) > 600 or re.search(r'[“”「」<>\"`]', value)
            or re.search(r'急救|报警|自杀|不想活|受伤|车祸|火灾|胸痛|去世|讣告|紧急', value)):
        return None
    candidates = None
    for pattern, choices in [
        (r'难过|伤心|想哭|委屈|不开心|不高兴|失落|心情不好|难受|烦躁', ['hug', 'comfort', 'here']),
        (r'呜呜|嘤嘤', ['sad', 'hug']),
        (r'辛苦|好累|太累|有点累|很累|加班|压力好大|忙死|困了', ['comfort', 'rest', 'hug']),
        (r'早安|早上好|早呀', ['morning']),
        (r'晚安|睡觉了|睡啦', ['goodnight']),
        (r'谢谢|感谢|多谢', ['thanks']),
        (r'哈哈|笑死|笑哭|好好笑|太逗|好开心|好高兴|太开心', ['laugh', 'cheer', 'okay']),
        (r'加油|能行|要努力|坚持住', ['cheer', 'okay']),
        (r'等等|等一下|稍等|等我', ['wait']),
        (r'出发|走啦|出门了|上班去|下班了', ['go', 'cheer']),
        (r'在吗|你好|嗨|聊聊|陪我|无聊|小满', ['here', 'hug', 'received']),
        (r'^(?:好的|好呀|好哒|行|嗯嗯|没问题|ok|可以)$', ['okay', 'received']),
    ]:
        if re.search(pattern, value):
            candidates = choices; break
    if candidates is None:
        # Ordinary conversation gets an image too; information-heavy questions
        # and urgent/sensitive situations keep the words in the foreground.
        if re.search(r'解释|代码|函数|sql|步骤|方案|分析|多少钱|是什么|为什么|如何|请问|怎么|不发.*表情|别.*表情|不要.*表情', value):
            return None
        candidates = ['here', 'received', 'okay']
    choices = [item for item in candidates if item != previous] or candidates
    return choices[int(hashlib.sha256(message_key.encode()).hexdigest()[:8], 16) % len(choices)]


def maybe_chat(db, owner, text, message_key):
    if not enabled():
        return
    pref = db.get(Preference, owner)
    now = utc_now()
    if not pref or not pref.chat_enabled:
        return
    previous = db.scalar(select(Delivery.sticker_id).where(Delivery.owner_id == owner, Delivery.source == 'chat').order_by(Delivery.id.desc()).limit(1))
    sticker = choose_sticker(text, message_key, previous)
    if not sticker:
        return
    enqueue(db, owner, sticker, "chat:"+message_key, "chat", now)


def queue_daily(db, bindings):
    if not enabled():
        return
    now=utc_now(); clock=local(now)
    owners={clawbot_owner(v["account_id"],v["sender_id"]) for v in bindings}
    for owner in sorted(owners):
        preference(db, owner, now)
    db.commit()
    if clock.hour != 9 or clock.minute >= 15:
        return
    for pref in db.scalars(select(Preference).where(Preference.daily_enabled.is_(True))):
        if (pref.owner_id not in owners or not pref.daily_started_at or local(pref.daily_started_at).date() >= clock.date()):
            continue
        expires=clock.replace(hour=9,minute=15,second=0,microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)
        enqueue(db, pref.owner_id, "morning", "daily:"+clock.date().isoformat(), "daily", now, expires)


def valid(db, item, owners, now):
    reason = None
    pref = db.get(Preference,item.owner_id)
    if item.owner_id not in owners:
        reason="binding_removed"
    elif item.expires_at <= now:
        reason="expired"
    elif item.source in {"daily","chat"} and (not pref or not getattr(pref,"daily_enabled" if item.source=="daily" else "chat_enabled")):
        reason="cancelled"
    elif not available(item.sticker_id):
        reason="asset_unavailable"
    if reason:
        item.status="cancelled"; item.error_code=reason; item.finished_at=now
    return not reason


def recover(db,now):
    for item in db.scalars(select(Delivery).where(Delivery.status.in_(["claimed","sending"]))):
        if item.status=="claimed" and item.claimed_at and now-item.claimed_at > timedelta(seconds=45):
            item.status="pending"; item.claim_hash=None
        elif item.status=="sending" and item.started_at and now-item.started_at > timedelta(seconds=120):
            item.status="unknown"; item.error_code="send_outcome_unknown"; item.finished_at=now
    db.commit()


def claim(db,bindings):
    if not enabled():
        raise HTTPException(503,"表情发送未启用")
    now=utc_now(); recover(db,now)
    mapping={clawbot_owner(v["account_id"],v["sender_id"]):v for v in bindings}
    for item in db.scalars(select(Delivery).where(Delivery.status=="pending").order_by(Delivery.id).limit(50)):
        if not valid(db,item,mapping,now):
            db.commit(); continue
        token=secrets.token_hex(32)
        changed=db.execute(update(Delivery).where(Delivery.id==item.id,Delivery.status=="pending").values(
            status="claimed",claimed_at=now,claim_hash=hashlib.sha256(token.encode()).hexdigest()))
        db.commit()
        if changed.rowcount==1:
            return {"id":item.id,"claim_token":token,**mapping[item.owner_id],"sticker_id":item.sticker_id,"text":""}
    return None


def leased(db,item_id,token):
    item=db.get(Delivery,item_id)
    if not item or not item.claim_hash or not hmac.compare_digest(item.claim_hash,hashlib.sha256(token.encode()).hexdigest()):
        raise HTTPException(409,"表情领取凭据无效")
    return item


def begin_send(db,item_id,token,bindings):
    if not enabled():
        raise HTTPException(503,"表情发送未启用")
    item=leased(db,item_id,token); now=utc_now()
    if item.status!="claimed" or not item.claimed_at or now-item.claimed_at>timedelta(seconds=45):
        raise HTTPException(409,"表情领取已失效")
    owners={clawbot_owner(v["account_id"],v["sender_id"]) for v in bindings}
    if not valid(db,item,owners,now):
        db.commit(); raise HTTPException(409,"绑定、偏好、素材或发送时机已变化")
    changed=db.execute(update(Delivery).where(Delivery.id==item.id,Delivery.status=="claimed",Delivery.claim_hash==item.claim_hash).values(status="sending",started_at=now))
    db.commit()
    if changed.rowcount!=1:
        raise HTTPException(409,"表情发送已经开始")


def complete_send(db,item_id,token,outcome,message_id,error_code):
    item=leased(db,item_id,token)
    if item.status==outcome:
        return
    if item.status == 'claimed' and outcome == 'failed' and error_code == 'asset_unavailable':
        item.status = 'failed'; item.error_code = error_code; item.finished_at = utc_now()
        db.commit()
        return
    if item.status not in {"sending","unknown"}:
        raise HTTPException(409,"表情发送状态不允许更新")
    if outcome=="accepted" and not message_id:
        raise HTTPException(422,"平台受理须有消息标识")
    item.status=outcome; item.external_message_id=message_id; item.error_code=error_code; item.finished_at=utc_now()
    db.commit()
