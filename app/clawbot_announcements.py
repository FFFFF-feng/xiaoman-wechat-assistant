"""Version-bound update notices. Never reuse onboarding or task records."""
from datetime import timedelta
import hashlib
import hmac
import os
import secrets
import time
from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from .clawbot_identity import clawbot_owner
from .models import ClawbotAnnouncement as Delivery, ClawbotRelease as Published, ClawbotWelcome
from . import release_catalog
from .schedule_access import utc_now
from .schemas import ChatResponse

_worker_seen = 0.0


def enabled():
    return (os.getenv('CLAWBOT_ENABLED', 'false').lower() == 'true'
            and os.getenv('CLAWBOT_ANNOUNCEMENTS_ENABLED', 'true').lower() == 'true')


def queue_release(db, bindings):
    """Publish the latest version once, atomically snapshotting current recipients."""
    if not enabled():
        return
    releases = release_catalog.catalog()
    item = releases[0]
    for known in db.scalars(select(Published)):
        current = next((value for value in releases if value.version == known.version), None)
        if current and hashlib.sha256(current.model_dump_json().encode()).hexdigest() != known.fingerprint:
            raise ValueError('Published content changed; increment the release version')
        if tuple(map(int, known.version.split('.'))) > tuple(map(int, item.version.split('.'))):
            return  # A code rollback must not announce an older version as new.
    content = item.model_dump_json()
    fingerprint = hashlib.sha256(content.encode()).hexdigest()
    existing = db.get(Published, item.version)
    if existing:
        if existing.fingerprint != fingerprint:
            raise ValueError('Published content changed; increment the release version')
        return
    owners = {clawbot_owner(v['account_id'], v['sender_id']) for v in bindings}
    now = utc_now()
    try:
        with db.begin_nested():
            db.add(Published(version=item.version, content_json=content, fingerprint=fingerprint, published_at=now))
            db.flush()
            for owner in sorted(owners):
                welcome = db.scalar(select(ClawbotWelcome).where(ClawbotWelcome.owner_id == owner))
                if welcome and welcome.status in {'waiting', 'pending', 'claimed', 'sending'}:
                    continue  # New members learn the current features from their welcome guide.
                db.add(Delivery(owner_id=owner, version=item.version, created_at=now,
                                expires_at=now + timedelta(days=7)))
            db.flush()
        db.commit()
    except IntegrityError:
        db.rollback()
        published = db.get(Published, item.version)
        if not published or published.fingerprint != fingerprint:
            raise


def command(text, base_url):
    import re
    value = re.sub(r'[\s，,。.!！?？]', '', text)
    if value not in {'更新公告', '更新板', '查看更新', '查看更新公告', '看看最近更新',
                     '最近更新了什么', '有什么新功能', '小满有什么新功能', '最新更新'}:
        return None
    item = release_catalog.catalog()[0]
    if not base_url:
        return ChatResponse(action='chat', reply=f'小满 v{item.version}：{item.summary}\n更新板地址暂时不可用，可以稍后再问我“最近更新了什么”。')
    return ChatResponse(action='chat', reply=release_catalog.notice(item, base_url))


def status(db):
    items = db.scalars(select(Published).order_by(Published.published_at.desc())).all()
    counts = db.execute(select(Delivery.version, Delivery.status, func.count()).group_by(Delivery.version, Delivery.status)).all()
    return {'enabled': enabled(), 'worker_online': enabled() and time.monotonic() - _worker_seen < 20,
            'releases': [{'version': item.version, 'counts': {state: count for version, state, count in counts if version == item.version}}
                         for item in items]}


def recover(db, now):
    for item in db.scalars(select(Delivery).where(Delivery.status.in_(['claimed', 'sending']))):
        if item.status == 'claimed' and item.claimed_at and now - item.claimed_at > timedelta(seconds=45):
            item.status = 'pending'; item.claim_hash = None
        elif item.status == 'sending' and item.started_at and now - item.started_at > timedelta(seconds=120):
            item.status = 'unknown'; item.error_code = 'send_outcome_unknown'; item.finished_at = now
    db.commit()


def valid(db, item, owners, now):
    reason = 'binding_removed' if item.owner_id not in owners else 'expired' if item.expires_at <= now else None
    if reason:
        item.status = 'blocked' if reason == 'binding_removed' else 'cancelled'
        item.error_code = reason; item.finished_at = now
    return not reason


def claim(db, bindings, base_url):
    global _worker_seen
    if not enabled():
        raise HTTPException(503, '更新通知已暂停')
    _worker_seen = time.monotonic()
    now = utc_now(); recover(db, now)
    if not base_url:
        return None
    mapping = {clawbot_owner(v['account_id'], v['sender_id']): v for v in bindings}
    for item in db.scalars(select(Delivery).where(Delivery.status == 'pending').order_by(Delivery.id).limit(200)):
        if not valid(db, item, mapping, now):
            db.commit(); continue
        release = release_catalog.Release.model_validate_json(db.get(Published, item.version).content_json)
        token = secrets.token_hex(32)
        changed = db.execute(update(Delivery).where(Delivery.id == item.id, Delivery.status == 'pending').values(
            status='claimed', claimed_at=now, claim_hash=hashlib.sha256(token.encode()).hexdigest()))
        db.commit()
        if changed.rowcount == 1:
            return {'id': item.id, 'claim_token': token, **mapping[item.owner_id],
                    'text': release_catalog.notice(release, base_url)}
    return None


def leased(db, item_id, token):
    item = db.get(Delivery, item_id)
    if not item or not item.claim_hash or not hmac.compare_digest(item.claim_hash, hashlib.sha256(token.encode()).hexdigest()):
        raise HTTPException(409, '更新通知领取凭据无效')
    return item


def begin_send(db, item_id, token, bindings):
    if not enabled():
        raise HTTPException(503, '更新通知已暂停')
    item = leased(db, item_id, token); now = utc_now()
    if item.status != 'claimed' or not item.claimed_at or now - item.claimed_at > timedelta(seconds=45):
        raise HTTPException(409, '更新通知领取已失效')
    owners = {clawbot_owner(v['account_id'], v['sender_id']) for v in bindings}
    if not valid(db, item, owners, now):
        db.commit(); raise HTTPException(409, '绑定或通知时效已变化')
    changed = db.execute(update(Delivery).where(Delivery.id == item.id, Delivery.status == 'claimed', Delivery.claim_hash == item.claim_hash).values(status='sending', started_at=now))
    db.commit()
    if changed.rowcount != 1:
        raise HTTPException(409, '更新通知已开始发送')


def complete_send(db, item_id, token, outcome, message_id, error_code):
    item = leased(db, item_id, token)
    if item.status == outcome:
        return
    if item.status not in {'sending', 'unknown'}:
        raise HTTPException(409, '更新通知状态不允许更新')
    if outcome == 'accepted' and not message_id:
        raise HTTPException(422, '平台受理须有消息标识')
    item.status = outcome; item.external_message_id = message_id; item.error_code = error_code
    item.finished_at = utc_now(); db.commit()
