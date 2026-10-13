"""Independent admin password and short-lived in-memory sessions; no bridge credentials."""
import base64
from collections import deque
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time

from fastapi import HTTPException, Request

ITERATIONS = 310000
SESSION_SECONDS = 3600
_lock = threading.RLock()
_sessions: dict[str, tuple[float, str]] = {}
_attempts = deque()


def password_record(password: str) -> dict:
    salt = secrets.token_bytes(24)
    value = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, ITERATIONS)
    return {'version': 1, 'username': 'admin', 'iterations': ITERATIONS,
            'salt': base64.b64encode(salt).decode(), 'digest': base64.b64encode(value).decode()}


def configuration() -> tuple[dict, str]:
    try:
        if os.getenv('CLAWBOT_ENABLED', 'false').lower() != 'true':
            raise ValueError()
        path = Path(os.environ['CLAWBOT_ADMIN_AUTH_FILE'])
        if not path.is_absolute() or path.is_symlink() or path != path.resolve() or path.stat().st_size > 8192:
            raise ValueError()
        if os.name != 'nt' and path.stat().st_mode & 0o077:
            raise ValueError()
        data = path.read_bytes()
        value = json.loads(data)
        if (set(value) != {'version','username','iterations','salt','digest'} or value['version'] != 1
                or value['username'] != 'admin' or value['iterations'] != ITERATIONS
                or len(base64.b64decode(value['salt'], validate=True)) != 24
                or len(base64.b64decode(value['digest'], validate=True)) != 32):
            raise ValueError()
        return value, hashlib.sha256(data).hexdigest()
    except (OSError, KeyError, ValueError, TypeError):
        raise HTTPException(503, '管理后台尚未配置，请联系部署管理员') from None


def check_origin(request: Request) -> None:
    origin = request.headers.get('origin')
    if origin is not None and origin != os.getenv('PUBLIC_BASE_URL', '').rstrip('/'):
        raise HTTPException(403, '请从本项目管理页面操作')


def login(request: Request, username: str, password: str) -> dict:
    check_origin(request)
    value, fingerprint = configuration()
    with _lock:
        now = time.monotonic()
        while _attempts and now - _attempts[0] >= 60:
            _attempts.popleft()
        if len(_attempts) >= 10:
            raise HTTPException(429, '登录尝试过于频繁，请一分钟后再试', headers={'Retry-After':'60'})
        _attempts.append(now)
        derived = hashlib.pbkdf2_hmac('sha256', password.encode(), base64.b64decode(value['salt']), ITERATIONS)
        matches = hmac.compare_digest(derived, base64.b64decode(value['digest']))
        if not matches or not hmac.compare_digest(username.encode(), b'admin'):
            raise HTTPException(401, '账号或密码不正确')
        for key, (expiry, previous) in list(_sessions.items()):
            if expiry <= now or previous != fingerprint:
                del _sessions[key]
        if len(_sessions) >= 8:
            del _sessions[min(_sessions, key=lambda key: _sessions[key][0])]
        token = secrets.token_urlsafe(32)
        _sessions[hashlib.sha256(token.encode()).hexdigest()] = (now + SESSION_SECONDS, fingerprint)
        return {'session':token, 'expires_in':SESSION_SECONDS, 'username':'admin'}


def authorize(request: Request) -> str:
    check_origin(request)
    _, fingerprint = configuration()
    header = request.headers.get('authorization', '')
    token = header[7:] if header.startswith('Bearer ') else ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        raise HTTPException(401, '请先登录管理后台')
    key = hashlib.sha256(token.encode()).hexdigest()
    with _lock:
        value = _sessions.get(key)
        if not value or value[0] <= time.monotonic() or value[1] != fingerprint:
            _sessions.pop(key, None)
            raise HTTPException(401, '登录已过期，请重新登录')
    return key


def logout(key: str) -> None:
    with _lock:
        _sessions.pop(key, None)
