"""Loopback-only public guide preview. No database, secrets or Weixin worker."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from fastapi import FastAPI
from app.guide_page import router

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(router)
if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=18086)
