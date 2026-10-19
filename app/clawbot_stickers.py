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
from .models import ClawbotStickerDelivery as Delivery, ClawbotStickerPreference as Preference, ClawbotStickerStyle as Style
from .schedule_access import utc_now
from .schemas import ChatResponse
from .sticker_catalog import CAPTIONS, PHOENIX, SCENES, STYLES, matches_style, scene_for

ASSETS = Path(__file__).resolve().parents[1] / "assets/stickers/phoenix-d"
LOCAL = ZoneInfo("Asia/Shanghai")


def style_for(db, owner):
    saved = db.get(Style, owner)
    return saved.style if saved and saved.style in STYLES else 'mixed'


def recent_stickers(db, owner):
    return list(db.scalars(select(Delivery.sticker_id).where(
        Delivery.owner_id == owner, Delivery.status.in_(['pending', 'claimed', 'sending', 'accepted'])
    ).order_by(Delivery.id.desc()).limit(3)))


def pick(scene, message_key, style='mixed', recent=()):
    candidates = [s for s in SCENES[scene] if matches_style(s, style) and available(s)]
    if not candidates:
        return None
    choices = [s for s in candidates if s not in recent]
    if not choices:
        choices = [s for s in candidates if not recent or s != recent[0]] or candidates
    return choices[int(hashlib.sha256(message_key.encode()).hexdigest()[:8], 16) % len(choices)]


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
    style_commands = {
        '只用小凤鸟表情': 'phoenix', '只用小凤鸟': 'phoenix', '表情风格小凤鸟': 'phoenix',
        '只用3D表情': 'fluent', '只用3d表情': 'fluent', '表情风格3D': 'fluent',
        '混合表情': 'mixed', '混合搭配表情': 'mixed', '表情风格随机': 'mixed',
    }
    if value in style_commands:
        chosen = style_commands[value]
        previous_style = style_for(db, owner)
        saved = db.get(Style, owner)
        if saved:
            saved.style = chosen
        else:
            db.add(Style(owner_id=owner, style=chosen))
        # Claimed images have not started native upload; invalidate their lease.
        if previous_style != chosen:
            db.execute(update(Delivery).where(Delivery.owner_id == owner,
                Delivery.source.in_(['chat', 'daily']), Delivery.status.in_(['pending', 'claimed'])
            ).values(status='cancelled', claim_hash=None, error_code='style_changed'))
        db.commit()
        return ChatResponse(action='chat', reply='记住啦，以后'+STYLES[chosen]+'。自动发送的开关保持你的原设置。')
    style = style_for(db, owner)
    names = '|'.join(map(re.escape, sorted([*CAPTIONS.values(), '早安', '谢谢'], key=len, reverse=True)))
    request = re.fullmatch(r"(?:给我(?:一个|一张)?|发个|来个|来张|发送|发一张)(" + names + r")(?:表情|表情包)", value)
    manual_id = None
    if request:
        caption = {"早安":"早呀", "谢谢":"谢谢你"}.get(request[1], request[1])
        manual_id = next(k for k,v in CAPTIONS.items() if v == caption)
    elif value in {"给我一个表情包", "发个表情包", "来个表情包", "发表情包"}:
        manual_id = "received"
    elif re.fullmatch(r'(?:怎么|咋|为什么)(?:还)?(?:不|没|没有)(?:给我)?发(?:发)?表情(?:包)?(?:呢|呀|啊)?', value):
        manual_id = "hug"
    elif value in {'来张猫猫表情', '发个猫猫表情包', '给我猫猫表情包'}:
        manual_id = 'fluent_cat_hello'
    elif value in {'换张表情', '换一张表情', '换个表情包', '换一种表情'}:
        manual_id = pick('neutral', message_key, style, recent_stickers(db, owner))
        if not manual_id:
            return ChatResponse(action='chat', reply='这个风格的表情暂时不可用，没有安排发送。')
    # Named external requests explicitly choose that image, even in phoenix mode.
    # Generic native requests follow fluent-only settings via an equivalent scene.
    if manual_id in PHOENIX and style == 'fluent':
        manual_id = pick(scene_for(manual_id), message_key, style, recent_stickers(db, owner)) or manual_id
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
        if not any(matches_style(s, style) and available(s) for s in CAPTIONS) or not pick('morning', message_key, style):
            return reply("表情素材还没准备齐，暂未开启。")
        if not pref.daily_enabled:
            pref.daily_started_at = utc_now()
        pref.chat_enabled = pref.daily_enabled = True
        db.commit()
        return reply("聊天配图和日常问候都已开启。聊天配图没有次数和冷却限制；早安按北京时间每天9点尝试发送，开启当天不补发。随时说“关闭表情包”。")
    if value in switches:
        field, on = switches[value]
        if on and (not any(matches_style(s, style) and available(s) for s in CAPTIONS)
                   or field == 'daily_enabled' and not pick('morning', message_key, style)):
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
        if manual_id in PHOENIX and style == 'fluent':
            return reply('这个风格的表情暂时不可用，没有安排发送。')
        if not enqueue(db, owner, manual_id, "manual:"+message_key, "manual", now):
            return reply("这张表情暂时不可用，没有安排发送。")
        return reply("“"+CAPTIONS[manual_id]+"”表情已加入发送队列。若图片没到，请稍后再看，平台受理不代表已经送达。")
    return reply("想看哪张，直接说“给我抱抱表情包”或“来张猫猫表情”。现在有："+'、'.join(CAPTIONS.values())+"。\n风格："+STYLES[style]+"，可说“只用小凤鸟表情”“只用3D表情”或“混合表情”。\n聊天配图："+
                 ("开" if pref.chat_enabled else "关")+"；每日9点问候："+("开" if pref.daily_enabled else "关")+
                 "。默认自动开启，不设次数和冷却限制。说“关闭表情包”暂停，说“开启表情包”恢复，也可分别开关聊天和日常表情。")


def choose_sticker(text, message_key, previous=None, *, style='mixed', recent=()):
    value = normalized(text).lower()
    if (len(value) > 600 or re.search(r'[“”「」<>\"`]', value)
            or re.search(r'急救|报警|自杀|不想活|受伤|车祸|火灾|胸痛|去世|讣告|紧急|服药|吃药|就医|看病|手术|葬礼', value)
            or re.search(r'解释|代码|函数|sql|步骤|方案|分析|多少钱|是什么|为什么|如何|请问|怎么|表情', value)):
        return None
    scene = 'neutral'
    for pattern, matched_scene in [
        (r'难过|伤心|想哭|委屈|不开心|不高兴|失落|心情不好|难受|烦躁', 'comfort'),
        (r'呜呜|嘤嘤', 'sad'),
        (r'辛苦|好累|太累|有点累|很累|加班|压力好大|忙死|困了', 'tired'),
        (r'早安|早上好|早呀', 'morning'),
        (r'晚安|睡觉了|睡啦', 'night'),
        (r'谢谢|感谢|多谢', 'thanks'),
        (r'哈哈|笑死|笑哭|好好笑|太逗|好开心|好高兴|太开心', 'happy'),
        (r'加油|能行|要努力|坚持住', 'cheer'),
        (r'等等|等一下|稍等|等我', 'wait'),
        (r'出发|走啦|出门了|上班去|下班了', 'go'),
        (r'喜欢你|爱你|贴贴', 'love'),
        (r'在吗|你好|嗨|聊聊|陪我|无聊|小满', 'hello'),
        (r'^(?:好的|好呀|好哒|行|嗯嗯|没问题|ok|可以)$', 'okay'),
    ]:
        if re.search(pattern, value):
            scene = matched_scene; break
    return pick(scene, message_key, style, recent or ([previous] if previous else []))


def maybe_chat(db, owner, text, message_key):
    if not enabled():
        return
    pref = db.get(Preference, owner)
    now = utc_now()
    if not pref or not pref.chat_enabled:
        return
    sticker = choose_sticker(text, message_key, style=style_for(db, owner), recent=recent_stickers(db, owner))
    if not sticker:
        return
    enqueue(db, owner, sticker, "chat:"+message_key, "chat", now)


def maybe_task_reply(db, owner, text, message_key, response):
    """React only to an actual successful task write, never to a model's promise."""
    task = response.task
    if (not enabled() or response.action not in {'create', 'snooze', 'complete'} or not task
            or (response.action == 'complete' and task.status != 'done')
            or (response.action != 'complete' and task.status not in {'open', 'snoozed'})
            or task.category == 'health'
            or choose_sticker(text + ' ' + task.title, message_key) is None):
        return
    pref = db.get(Preference, owner)
    if not pref or not pref.chat_enabled:
        return
    scene = 'celebrate' if response.action == 'complete' else 'okay'
    sticker = pick(scene, message_key, style_for(db, owner), recent_stickers(db, owner))
    if sticker:
        enqueue(db, owner, sticker, 'chat:' + message_key, 'chat', utc_now())


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
        style = style_for(db, pref.owner_id)
        event = "daily:"+clock.date().isoformat()
        sticker = 'morning' if style != 'fluent' and available('morning') else pick('morning', event, style)
        if sticker:
            enqueue(db, pref.owner_id, sticker, event, "daily", now, expires)


def valid(db, item, owners, now):
    reason = None
    pref = db.get(Preference,item.owner_id)
    if item.owner_id not in owners:
        reason="binding_removed"
    elif item.expires_at <= now:
        reason="expired"
    elif item.source in {"daily","chat"} and (not pref or not getattr(pref,"daily_enabled" if item.source=="daily" else "chat_enabled")):
        reason="cancelled"
    elif item.source in {'daily', 'chat'} and not matches_style(item.sticker_id, style_for(db, item.owner_id)):
        reason='style_changed'
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
