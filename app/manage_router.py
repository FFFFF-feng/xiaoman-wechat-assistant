"""Narrow browser administration surface. Never exposes native credentials or internal RPC."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import admin_auth, clawbot_delivery, clawbot_enrollment as enrollment
from .clawbot_bindings import load_bindings, lock
from .clawbot_identity import clawbot_owner, is_clawbot_owner
from .database import get_db
from .models import ClawbotInvitation, ClawbotMemberProfile, ClawbotAdminIssue, ClawbotAdminEvent, Task
from .schedule_access import utc_now

router = APIRouter(prefix='/api/manage', include_in_schema=False)


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Login(Strict):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class Label(Strict):
    label: str = Field(min_length=1, max_length=60, pattern=r'^[^\x00-\x1f\x7f]+$')


class Issue(Label):
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')


async def payload(request: Request, model):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 4096:
            raise HTTPException(413, '提交内容过长')
    try:
        value = model.model_validate_json(bytes(data))
        if hasattr(value, 'label') and not value.label.strip():
            raise ValueError()
        return value
    except (ValidationError, ValueError):
        raise HTTPException(400, '请检查填写内容，不要提交身份或额外字段') from None


def event(db: Session, action: str, target: str, label: str | None = None):
    db.add(ClawbotAdminEvent(action=action, target=target, label=label, created_at=utc_now()))


def require_member(member_id: str):
    if not is_clawbot_owner(member_id):
        raise HTTPException(404, '成员不存在')
    for value in load_bindings():
        if clawbot_owner(value['account_id'], value['sender_id']) == member_id:
            return value
    raise HTTPException(404, '成员不存在或已撤销')


def native_state(binding: dict) -> str:
    """File consistency only; does not claim polling or phone delivery is healthy."""
    try:
        private = Path(os.environ['CLAWBOT_BINDINGS_FILE']).resolve().parent
        native = private.parent / 'openclaw-weixin'
        index = native / 'accounts.json'
        if index.is_symlink() or index.stat().st_size > 256 * 1024:
            return 'unknown'
        ids = json.loads(index.read_text(encoding='utf-8'))
        account_id = binding['account_id']
        if not re.fullmatch(r'[a-z0-9_-]{1,160}', account_id):
            return 'unknown'
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            return 'unknown'
        if account_id not in ids:
            return 'missing'
        account = native / 'accounts' / (account_id + '.json')
        if account.is_symlink() or account.stat().st_size > 64 * 1024:
            return 'unknown'
        value = json.loads(account.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            return 'unknown'
        if value.get('userId') != binding['sender_id'] or not isinstance(value.get('token'), str) or not value['token'].strip():
            return 'mismatch'
        return 'ready'
    except FileNotFoundError:
        return 'missing'
    except (OSError, ValueError, TypeError, KeyError):
        return 'unknown'


@router.post('/login')
async def login(request: Request, response: Response):
    value = await payload(request, Login)
    response.headers['Cache-Control'] = 'no-store'
    return admin_auth.login(request, value.username, value.password)


@router.post('/logout')
def logout(response: Response, session: str = Depends(admin_auth.authorize)):
    admin_auth.logout(session)
    response.headers['Cache-Control'] = 'no-store'
    return {'ok': True}


@router.get('/overview')
def overview(response: Response, session: str = Depends(admin_auth.authorize), db: Session = Depends(get_db)):
    response.headers['Cache-Control'] = 'no-store'
    with lock:
        enrollment.cleanup(db)
        values = load_bindings()
        labels = {row.owner_id:row.label for row in db.scalars(select(ClawbotInvitation)
                  .where(ClawbotInvitation.state == 'connected').order_by(ClawbotInvitation.id)).all()}
        labels.update({row.owner_id:row.label for row in db.scalars(select(ClawbotMemberProfile)).all()})
        totals = dict(db.execute(select(Task.owner_id, func.count()).group_by(Task.owner_id)).all())
        members = [{'member_id':clawbot_owner(v['account_id'],v['sender_id']),
                    'label':labels.get(clawbot_owner(v['account_id'],v['sender_id']), '初始成员 '+str(i+1)),
                    'authorization':native_state(v),
                    'task_count':totals.get(clawbot_owner(v['account_id'],v['sender_id']),0)} for i,v in enumerate(values)]
        now = utc_now()
        return {'members':members, 'limit':enrollment.capacity(),
                'worker_online':bool(enrollment._worker_seen and (now-enrollment._worker_seen).total_seconds()<15),
                'reminder_worker_online':clawbot_delivery.status()['worker_online'],
                'waiting_count':db.scalar(select(func.count()).select_from(ClawbotInvitation)
                    .where(ClawbotInvitation.state.in_(enrollment.ACTIVE))) or 0}


@router.post('/members/{member_id}/label')
async def rename(member_id: str, request: Request, session: str = Depends(admin_auth.authorize), db: Session = Depends(get_db)):
    value = await payload(request, Label)
    with lock:
        require_member(member_id)
        row = db.get(ClawbotMemberProfile, member_id)
        if not row:
            row = ClawbotMemberProfile(owner_id=member_id, label=value.label.strip(), updated_at=utc_now())
            db.add(row)
        else:
            row.label, row.updated_at = value.label.strip(), utc_now()
        event(db, 'rename_member', member_id, value.label.strip())
        db.commit()
    return {'ok':True}


@router.post('/members/{member_id}/revoke')
def revoke_member(member_id: str, session: str = Depends(admin_auth.authorize), db: Session = Depends(get_db)):
    with lock:
        require_member(member_id)
        event(db, 'revoke_member', member_id)
        enrollment.revoke_owner(db, member_id)
    return {'ok':True}


@router.get('/invitations')
def invitations(response: Response, session: str = Depends(admin_auth.authorize), db: Session = Depends(get_db)):
    response.headers['Cache-Control'] = 'no-store'
    with lock:
        enrollment.cleanup(db)
        rows = db.scalars(select(ClawbotInvitation).order_by(ClawbotInvitation.id.desc()).limit(100)).all()
        return {'invitations':[{'id':r.id,'label':r.label,'state':r.state,
                'created_at':r.created_at.isoformat()+'Z','expires_at':r.expires_at.isoformat()+'Z',
                'error_code':r.error_code} for r in rows]}


@router.post('/invitations')
async def issue(request: Request, session: str = Depends(admin_auth.authorize), db: Session = Depends(get_db)):
    value = await payload(request, Issue)
    enrollment.enabled()
    with lock:
        enrollment.cleanup(db)
        if db.get(ClawbotAdminIssue, value.request_id):
            raise HTTPException(409, '此邀请已创建，不能重复签发。请刷新记录；若链接未保存，取消旧邀请后重新生成')
        count = db.scalar(select(func.count()).select_from(ClawbotInvitation).where(ClawbotInvitation.expires_at>utc_now())) or 0
        if count >= 200:
            raise HTTPException(429, '短期邀请数量已达上限，请取消不需要的邀请或稍后再试')
        if len(load_bindings()) >= enrollment.capacity():
            raise HTTPException(409, '成员名额已满，请先核对成员，不要重复扫码已有用户')
        token = secrets.token_urlsafe(32)
        row = ClawbotInvitation(token_hash=enrollment.digest(token),label=value.label.strip(),state='issued',
                                created_at=utc_now(),expires_at=utc_now()+timedelta(minutes=30))
        db.add(row); db.flush()
        db.add(ClawbotAdminIssue(request_id=value.request_id,invitation_id=row.id,created_at=utc_now()))
        event(db,'issue_invite',str(row.id),row.label)
        db.commit()
        return {'id':row.id,'label':row.label,'invite':token,'expires_at':row.expires_at.isoformat()+'Z'}


@router.post('/invitations/{invitation_id}/cancel')
def cancel(invitation_id: int, session: str = Depends(admin_auth.authorize), db: Session = Depends(get_db)):
    with lock:
        enrollment.cleanup(db)
        row = db.get(ClawbotInvitation,invitation_id)
        if not row:
            raise HTTPException(404,'邀请不存在')
        if row.state=='connected':
            raise HTTPException(409,'这位用户已接入，请在成员页撤销，不要取消已完成的授权')
        if row.state in enrollment.ACTIVE or row.state=='issued':
            row.state,row.qr_image='cancelled',None
            enrollment._codes.pop(row.id,None)
            event(db,'cancel_invite',str(row.id),row.label)
            db.commit()
    return {'ok':True}


@router.get('/events')
def events(response: Response, session: str = Depends(admin_auth.authorize), db: Session = Depends(get_db)):
    response.headers['Cache-Control'] = 'no-store'
    rows=db.scalars(select(ClawbotAdminEvent).order_by(ClawbotAdminEvent.id.desc()).limit(50)).all()
    return {'events':[{'id':r.id,'action':r.action,'label':r.label,'created_at':r.created_at.isoformat()+'Z'} for r in rows]}
