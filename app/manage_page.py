"""Static admin login/workbench; personal data arrives only after independent authentication."""
from functools import lru_cache
from fastapi.responses import HTMLResponse
from .schedule_page import WEB, _hash


@lru_cache(maxsize=4)
def _bundle(version):
    html, css, js = [(WEB / f'manage.{suffix}').read_text(encoding='utf-8') for suffix in ('html','css','js')]
    if '</script' in js.lower() or '</style' in css.lower():
        raise ValueError('Unexpected closing tag in admin assets')
    html = html.replace('<link rel="stylesheet" href="/static/manage.css" />', f'<style>{css}</style>')
    html = html.replace('<script src="/static/manage.js" defer></script>', f'<script>{js}</script>')
    csp = (f"default-src 'none'; script-src 'sha256-{_hash(js)}'; style-src 'sha256-{_hash(css)}'; "
           "img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
    return html, csp


def manage_page_response():
    files = [WEB / f'manage.{suffix}' for suffix in ('html','css','js')]
    html, csp = _bundle(tuple((p.stat().st_mtime_ns,p.stat().st_size) for p in files))
    return HTMLResponse(html,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer',
        'Content-Security-Policy':csp,'X-Frame-Options':'DENY','X-Content-Type-Options':'nosniff'})
