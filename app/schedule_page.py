"""One-response schedule page with trusted local assets and a hash-based CSP."""
import base64
import hashlib
from functools import lru_cache
from pathlib import Path

from fastapi.responses import HTMLResponse

WEB = Path(__file__).resolve().parent.parent / "web"


def _hash(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


@lru_cache(maxsize=4)
def _bundle(_version: tuple) -> tuple[str, str]:
    html = (WEB / "schedule.html").read_text(encoding="utf-8")
    css = (WEB / "schedule.css").read_text(encoding="utf-8")
    js = (WEB / "schedule.js").read_text(encoding="utf-8")
    if "</script" in js.lower() or "</style" in css.lower():
        raise ValueError("Trusted schedule assets contain an unexpected closing tag")
    html = html.replace('<link rel="stylesheet" href="/static/schedule.css" />', f"<style>{css}</style>")
    html = html.replace('<script src="/static/schedule.js" defer></script>', f"<script>{js}</script>")
    csp = (f"default-src 'none'; script-src 'self' 'sha256-{_hash(js)}'; "
           f"style-src 'self' 'sha256-{_hash(css)}'; img-src 'self' data:; "
           "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
    return html, csp


def schedule_page_response() -> HTMLResponse:
    files = [WEB / name for name in ("schedule.html", "schedule.css", "schedule.js")]
    html, csp = _bundle(tuple((p.stat().st_mtime_ns, p.stat().st_size) for p in files))
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": csp, "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff"})
