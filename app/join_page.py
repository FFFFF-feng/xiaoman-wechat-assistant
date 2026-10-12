"""Invitation page served in one response, with no third-party JavaScript."""
from functools import lru_cache

from fastapi.responses import HTMLResponse
from .schedule_page import WEB, _hash


@lru_cache(maxsize=4)
def _bundle(version: tuple):
    html, css, js = [(WEB / f"join.{suffix}").read_text(encoding="utf-8") for suffix in ("html", "css", "js")]
    if "</script" in js.lower() or "</style" in css.lower():
        raise ValueError("Unexpected closing tag in trusted assets")
    html = html.replace('<link rel="stylesheet" href="/static/join.css" />', f"<style>{css}</style>")
    html = html.replace('<script src="/static/join.js" defer></script>', f"<script>{js}</script>")
    csp = (f"default-src 'none'; script-src 'sha256-{_hash(js)}'; style-src 'sha256-{_hash(css)}'; "
           "img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
    return html, csp


def join_page_response():
    files = [WEB / f"join.{suffix}" for suffix in ("html", "css", "js")]
    html, csp = _bundle(tuple((p.stat().st_mtime_ns, p.stat().st_size) for p in files))
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": csp, "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff"})
