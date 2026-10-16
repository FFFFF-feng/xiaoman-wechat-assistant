"""Local-only synthetic schedule preview; never loads real DB or starts a sender."""
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
preview_env = PROJECT / '.cache/schedule-preview.env'
preview_env.parent.mkdir(exist_ok=True)
preview_env.write_text('DATABASE_URL=sqlite://\nMODEL_ENABLED=false\nASR_ENABLED=false\n'
    'WECHAT_ENABLED=false\nWECHAT_DIALOGUE_ENABLED=false\nCLAWBOT_ENABLED=false\n'
    'CLAWBOT_REMINDERS_ENABLED=false\nCLAWBOT_ENROLLMENT_ENABLED=false\n', encoding='utf-8')
os.environ['XIAOMAN_ENV_FILE'] = str(preview_env)

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.database import Base, get_db
from app.models import Task
from app.schedule_access import schedule_owner
from app.schedule_page import schedule_page_response
from app.schedule_router import router

engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
Base.metadata.create_all(engine)
sessions = sessionmaker(engine, expire_on_commit=False)
owner = 'preview:local'
today = datetime.now(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None, second=0, microsecond=0)
with sessions() as db:
    for title, hour, minute, category, priority, status, notes, reminder in [
        ('给自己留一份认真吃的早餐', 9, 0, 'health', 'normal', 'done', '', False),
        ('把这周的小进展写进周报', 15, 30, 'work', 'high', 'open', '先列三件做过的事，再各补一句结果。不用一口气写完。', True),
        ('下楼走走，顺便取个快递', 21, 0, 'life', 'normal', 'open', '带上取件码，走一条喜欢的小路。', True),
        ('读几页一直想看的书', 22, 15, 'study', 'low', 'open', '', False),
    ]:
        db.add(Task(owner_id=owner, title=title, due_at=today.replace(hour=hour, minute=minute),
                    category=category, priority=priority, status=status, notes=notes, reminder_enabled=reminder))
    db.add(Task(owner_id=owner, title='去公园散步，看看秋天', due_at=(today+timedelta(days=1)).replace(hour=16, minute=0),
                category='life', notes='带一瓶水，不赶时间。', priority='normal', reminder_enabled=False))
    db.commit()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(router)
def get_preview_db():
    with sessions() as db:
        yield db
app.dependency_overrides[get_db] = get_preview_db
app.dependency_overrides[schedule_owner] = lambda: owner

@app.get('/schedule')
def preview():
    response = schedule_page_response()
    body = response.body.decode().replace('<body>', '<body><div class="preview-banner">界面预览 · 合成日程，所有操作都不会发送微信</div>', 1)
    return HTMLResponse(body, headers={k:v for k,v in response.headers.items() if k != 'content-length'})

if __name__ == '__main__':
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(); parser.add_argument('--port', type=int, default=18087)
    args = parser.parse_args()
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)
