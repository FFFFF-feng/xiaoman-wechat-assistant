"""Public help only: no identity token, private data, analytics or remote assets."""
from functools import lru_cache
from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse
from .schedule_page import WEB, _hash

router = APIRouter(include_in_schema=False)


@lru_cache(maxsize=4)
def _bundle(version):
    html, css, js = [(WEB / f"guide.{suffix}").read_text(encoding="utf-8")
                     for suffix in ("html", "css", "js")]
    if "</script" in js.lower() or "</style" in css.lower():
        raise ValueError("Unexpected closing tag in trusted assets")
    html = html.replace('<link rel="stylesheet" href="/static/guide.css" />', f"<style>{css}</style>")
    html = html.replace('<script src="/static/guide.js" defer></script>', f"<script>{js}</script>")
    csp = (f"default-src 'none'; script-src 'sha256-{_hash(js)}'; style-src 'sha256-{_hash(css)}'; "
           "img-src 'self'; connect-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
    return html, csp


@router.get("/guide")
def guide_page_response():
    files = [WEB / f"guide.{suffix}" for suffix in ("html", "css", "js")]
    html, csp = _bundle(tuple((p.stat().st_mtime_ns, p.stat().st_size) for p in files))
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": csp, "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff"})


@router.get("/static/xiaoman-avatar.jpg")
def guide_avatar():
    # Preserve the public legacy URL and proxy allowlist; serve the selected PNG unchanged.
    return FileResponse(WEB / "xiaoman-avatar.png", media_type="image/png",
        filename="小满头像.png", content_disposition_type="inline",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                 "Referrer-Policy": "no-referrer"})
