"""Invitation-gated enrollment. Public callers cannot choose a Weixin identity."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .clawbot_bindings import IDENTITY, load_bindings, lock, save_bindings
from .clawbot_identity import clawbot_owner
from .clawbot_router import authenticate
from .database import get_db
from .models import ClawbotInvitation, ScheduleAccess
from .schedule_access import utc_now

public_router = APIRouter(prefix="/api/join", include_in_schema=False)
admin_router = APIRouter(prefix="/api/internal/clawbot/enrollment", include_in_schema=False,
                         dependencies=[Depends(authenticate)])
ACTIVE = {"queued", "starting", "awaiting_scan", "needs_code"}
TERMINAL = {"connected", "failed", "cancelled", "expired", "revoked"}
_codes: dict[int, str] = {}  # Ephemeral, consumed once. Never logged or stored in SQLite.
_worker_seen = None


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def enabled() -> None:
    if os.getenv("CLAWBOT_ENROLLMENT_ENABLED", "false").lower() != "true":
        raise HTTPException(503, "多人接入暂未启用，请联系管理员")


def capacity() -> int:
    return max(1, min(200, int(os.getenv("CLAWBOT_MAX_USERS", "10"))))


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Issue(Strict):
    label: str = Field(min_length=1, max_length=60, pattern=r"^[^\x00-\x1f\x7f]+$")


class Code(Strict):
    code: str = Field(pattern=r"^[0-9]{2,12}$")


class Consent(Strict):
    consent: Literal[True]


class Lease(Strict):
    lease: str = Field(pattern=r"^[a-f0-9]{64}$")


class Update(Lease):
    state: Literal["starting", "awaiting_scan", "needs_code", "failed"]
    qr_image: str | None = Field(default=None, max_length=150000,
                                pattern=r"^data:image/png;base64,[A-Za-z0-9+/=]+$")
    error_code: Literal["login_failed", "login_timeout", "identity_ambiguous", "worker_stopped"] | None = None


class Complete(Lease):
    account_id: str = Field(min_length=1, max_length=160, pattern=IDENTITY.pattern)
    sender_id: str = Field(min_length=1, max_length=160, pattern=IDENTITY.pattern)


def cleanup(db: Session) -> None:
    now = utc_now()
    rows = db.scalars(select(ClawbotInvitation).where(ClawbotInvitation.state.not_in(TERMINAL))).all()
    for row in rows:
        if row.expires_at <= now or (row.deadline and row.deadline <= now):
            row.state = "expired"
        elif row.lease_until and row.lease_until <= now:
            # Never restart an uncertain native login after worker/backend failure.
            row.state, row.error_code = "failed", "worker_stopped"
        if row.state in TERMINAL:
            row.qr_image = None
            _codes.pop(row.id, None)
    db.commit()


def invitation(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> ClawbotInvitation:
    enabled()
    token = (authorization or "").removeprefix("Bearer ")
    if not authorization or not authorization.startswith("Bearer ") or not 40 <= len(token) <= 100:
        raise HTTPException(401, "请打开管理员发给你的完整邀请链接")
    with lock:
        cleanup(db)
        row = db.scalar(select(ClawbotInvitation).where(ClawbotInvitation.token_hash == digest(token)))
        if not row or row.expires_at <= utc_now():
            raise HTTPException(401, "邀请已过期或无效，请联系管理员领取新邀请")
        return row


def view(row: ClawbotInvitation) -> dict:
    return {"state": row.state, "expires_at": row.expires_at.isoformat() + "Z",
            "qr_image": row.qr_image if row.state in ACTIVE else None, "error_code": row.error_code}


@public_router.get("/status")
def status(response: Response, row: ClawbotInvitation = Depends(invitation)):
    response.headers["Cache-Control"] = "no-store"
    return view(row)


@public_router.post("/start")
def start(payload: Consent, row: ClawbotInvitation = Depends(invitation), db: Session = Depends(get_db)):
    with lock:
        db.refresh(row)
        if row.state != "issued":
            return view(row)  # A repeated click cannot spawn another login.
        if not _worker_seen or (utc_now() - _worker_seen).total_seconds() > 15:
            raise HTTPException(503, "扫码服务未在线，请联系管理员启动接入服务")
        reserved = db.scalars(select(ClawbotInvitation).where(ClawbotInvitation.state.in_(ACTIVE))).all()
        if len(load_bindings()) + len(reserved) >= capacity():
            raise HTTPException(409, "本轮体验名额已满，请联系管理员")
        row.state, row.consented_at = "queued", utc_now()
        db.commit()
        return view(row)


@public_router.post("/code")
def submit_code(payload: Code, row: ClawbotInvitation = Depends(invitation), db: Session = Depends(get_db)):
    with lock:
        db.refresh(row)
        if row.state != "needs_code" or row.id in _codes:
            raise HTTPException(409, "当前不需要配对码，请刷新状态")
        _codes[row.id] = payload.code
        return {"ok": True}


@public_router.post("/cancel")
def cancel(row: ClawbotInvitation = Depends(invitation), db: Session = Depends(get_db)):
    with lock:
        db.refresh(row)
        if row.state in ACTIVE or row.state == "issued":
            row.state, row.qr_image = "cancelled", None
            _codes.pop(row.id, None)
            db.commit()
        return view(row)


def revoke_owner(db: Session, owner: str) -> None:
    values = load_bindings()
    save_bindings([v for v in values if clawbot_owner(v["account_id"], v["sender_id"]) != owner])
    db.execute(delete(ScheduleAccess).where(ScheduleAccess.owner_id == owner))
    for row in db.scalars(select(ClawbotInvitation).where(ClawbotInvitation.owner_id == owner)).all():
        row.state, row.qr_image = "revoked", None
    db.commit()  # Tasks/history stay intact; delivery rechecks the allowlist before sending.


@public_router.post("/revoke")
def revoke(row: ClawbotInvitation = Depends(invitation), db: Session = Depends(get_db)):
    with lock:
        db.refresh(row)
        if row.state == "connected" and row.owner_id:
            revoke_owner(db, row.owner_id)
        return view(row)


@admin_router.post("/invites")
def issue(payload: Issue, db: Session = Depends(get_db)):
    enabled()
    with lock:
        cleanup(db)
        count = len(db.scalars(select(ClawbotInvitation).where(ClawbotInvitation.expires_at > utc_now())).all())
        if count >= 200:
            raise HTTPException(429, "短期邀请数量已达上限")
        token = secrets.token_urlsafe(32)
        row = ClawbotInvitation(token_hash=digest(token), label=payload.label.strip(),
                                state="issued", created_at=utc_now(), expires_at=utc_now() + timedelta(minutes=30))
        db.add(row)
        db.commit()
        return {"id": row.id, "invite": token, "expires_at": row.expires_at.isoformat() + "Z"}


@admin_router.get("/members")
def members(db: Session = Depends(get_db)):
    labels = {row.owner_id: row.label for row in db.scalars(select(ClawbotInvitation).where(ClawbotInvitation.state == "connected")).all()}
    return {"members": [{"member_id": clawbot_owner(v["account_id"], v["sender_id"]),
                         "label": labels.get(clawbot_owner(v["account_id"], v["sender_id"]), "初始账号")} for v in load_bindings()],
            "limit": capacity(), "worker_online": bool(_worker_seen and (utc_now() - _worker_seen).total_seconds() < 15)}


@admin_router.post("/members/{member_id}/revoke")
def admin_revoke(member_id: str, db: Session = Depends(get_db)):
    with lock:
        if member_id not in {clawbot_owner(v["account_id"], v["sender_id"]) for v in load_bindings()}:
            raise HTTPException(404, "成员不存在")
        revoke_owner(db, member_id)
        return {"ok": True}


@admin_router.post("/claim")
def claim(db: Session = Depends(get_db)):
    global _worker_seen
    enabled()
    with lock:
        _worker_seen = utc_now()
        cleanup(db)
        if db.scalar(select(ClawbotInvitation).where(ClawbotInvitation.state.in_(ACTIVE - {"queued"}))):
            return {"job": None}  # Exactly one native login at a time.
        row = db.scalar(select(ClawbotInvitation).where(ClawbotInvitation.state == "queued").order_by(ClawbotInvitation.id))
        if not row:
            return {"job": None}
        lease = secrets.token_hex(32)
        row.lease_hash, row.lease_until = digest(lease), utc_now() + timedelta(seconds=30)
        row.state, row.deadline = "starting", min(row.expires_at, utc_now() + timedelta(minutes=8))
        db.commit()
        return {"job": {"id": row.id, "lease": lease}}


def leased(db: Session, job_id: int, lease: str) -> ClawbotInvitation:
    cleanup(db)
    row = db.get(ClawbotInvitation, job_id)
    if not row or not row.lease_hash or not hmac.compare_digest(row.lease_hash, digest(lease)):
        raise HTTPException(403, "接入租约无效")
    if row.state not in ACTIVE - {"queued"}:
        raise HTTPException(409, "接入会话已停止")
    return row


@admin_router.post("/{job_id}/poll")
def worker_poll(job_id: int, payload: Lease, db: Session = Depends(get_db)):
    global _worker_seen
    with lock:
        row = leased(db, job_id, payload.lease)
        _worker_seen = utc_now()
        row.lease_until = utc_now() + timedelta(seconds=30)
        code = _codes.pop(row.id, None)
        if code:
            row.state = "awaiting_scan"
        db.commit()
        return {"code": code}


@admin_router.post("/{job_id}/update")
def worker_update(job_id: int, payload: Update, db: Session = Depends(get_db)):
    with lock:
        row = leased(db, job_id, payload.lease)
        row.state = payload.state
        if payload.qr_image:
            row.qr_image = payload.qr_image
        if payload.state == "failed":
            row.qr_image, row.error_code = None, payload.error_code or "login_failed"
            _codes.pop(row.id, None)
        db.commit()
        return {"ok": True}


@admin_router.post("/{job_id}/complete")
def worker_complete(job_id: int, payload: Complete, db: Session = Depends(get_db)):
    with lock:
        row = db.get(ClawbotInvitation, job_id)
        owner = clawbot_owner(payload.account_id, payload.sender_id)
        if (row and row.state == "connected" and row.owner_id == owner
                and hmac.compare_digest(row.lease_hash or "", digest(payload.lease))):
            return {"ok": True}  # Native login is never repeated after a lost HTTP acknowledgment.
        row = leased(db, job_id, payload.lease)
        values = load_bindings()
        if any(v["sender_id"] == payload.sender_id or v["account_id"] == payload.account_id for v in values):
            raise HTTPException(409, "微信已接入，不能覆盖原绑定；如需迁移请联系管理员")
        if len(values) >= capacity():
            raise HTTPException(409, "体验名额已满")
        save_bindings(values + [{"account_id": payload.account_id, "sender_id": payload.sender_id}])
        row.owner_id, row.state, row.qr_image = owner, "connected", None
        _codes.pop(row.id, None)
        try:
            db.commit()
        except Exception:
            db.rollback()
            save_bindings(values)
            raise
        return {"ok": True}
